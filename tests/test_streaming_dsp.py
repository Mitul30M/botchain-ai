import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from app.api.v1.messages import _assistant_stream, _chat_lock
from app.streaming import UI_STREAM_HEADERS, _ui_stream


async def _collect_events(stream):
    """Consume an encoder stream; return parsed data lines (None for [DONE])."""
    events = []
    async for chunk in stream:
        text = chunk.decode("utf-8")
        assert text.startswith("data: ")
        payload = text[len("data: ") :].strip()
        events.append(None if payload == "[DONE]" else json.loads(payload))
    return events


async def test_ui_stream_headers_match_sdk_requirements():
    assert UI_STREAM_HEADERS["content-type"] == "text/event-stream"
    assert UI_STREAM_HEADERS["x-vercel-ai-ui-message-stream"] == "v1"
    assert UI_STREAM_HEADERS["cache-control"] == "no-cache"
    assert UI_STREAM_HEADERS["connection"] == "keep-alive"
    assert UI_STREAM_HEADERS["x-accel-buffering"] == "no"


async def _tagged(*items):
    for item in items:
        yield item


async def test_ui_stream_order_and_validity():
    events = await _collect_events(
        _ui_stream(
            _tagged(
                ("status", "Planning…"),
                ("text", "Hello "),
                ("text", "world"),
                ("status", "Workflow validated. Done."),
            )
        )
    )

    types = [None if e is None else e["type"] for e in events]
    assert types == [
        "start",
        "data-status",
        "text-start",
        "text-delta",
        "text-delta",
        "text-end",
        "data-status",
        "finish",
        None,
    ]
    assert types.count(None) == 1

    text_deltas = [e["delta"] for e in events if e and e["type"] == "text-delta"]
    assert "".join(text_deltas) == "Hello world"

    statuses = [e["data"]["status"] for e in events if e and e["type"] == "data-status"]

    assert statuses == ["Planning…", "Workflow validated. Done."]
    for e in events:
        if e and e["type"] == "data-status":
            assert e["transient"] is True

    assert events[-2] == {"type": "finish", "finishReason": "stop"}


async def test_ui_stream_statuses_never_become_text():
    events = await _collect_events(_ui_stream(_tagged(("status", "Planning…"))))

    types = [e["type"] for e in events if e]
    assert types == ["start", "data-status", "finish"]
    assert not any(e and e["type"] in ("text-start", "text-delta") for e in events)
    assert events[-1] is None


async def test_ui_stream_errors_emit_error_chunk_and_re_raise():
    async def _boom():
        yield ("status", "Planning…")
        raise RuntimeError("simulated agent crash")

    collected = []

    with pytest.raises(RuntimeError, match="simulated agent crash"):
        async for chunk in _ui_stream(_boom()):
            text = chunk.decode("utf-8")
            if text.startswith("data: "):
                payload = text[len("data: ") :].strip()
                collected.append(None if payload == "[DONE]" else json.loads(payload))

    types = [e["type"] for e in collected if e]
    assert types == ["start", "data-status", "error"]
    assert "errorText" in collected[-1]
    assert None not in collected
    assert not any(e and e["type"] == "finish" for e in collected)


class _FakeState:
    def __init__(self, values, tasks=None):
        self.values = values
        self.tasks = tasks or []


class _FakeAgent:
    def __init__(self, state_values, events):
        self._values = state_values
        self._events = events

    async def astream(self, input, config, stream_mode):
        for event in self._events:
            yield event

    async def aget_state(self, config):
        return _FakeState(self._values)


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeSession:
    def __init__(self):
        self._added = []
        self.committed = False

    async def execute(self, stmt):
        return _FakeResult(None)

    def add(self, message):
        self._added.append(message)

    async def flush(self):
        for row in self._added:
            if getattr(row, "id", None) is None:
                row.id = f"m-{self._added.index(row) + 1}"

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_assistant_stream_full_run_decodes_to_persisted_text(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(
        "app.api.v1.messages.get_session_factory", lambda: lambda: session
    )

    agent = _FakeAgent(
        state_values={"messages": [AIMessage(content="plan reply")], "phase": "plan"},
        events=[
            ("custom", {"status": "Planning…"}),
            ("messages", (AIMessage(content="plan reply"), {})),
        ],
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(agent=agent))
    )
    lock = _chat_lock("chat-1")
    await lock.acquire()

    events = await _collect_events(
        _ui_stream(_assistant_stream(request, "chat-1", "hi", lock))
    )

    types = [e["type"] for e in events if e]
    assert types == [
        "start",
        "data-status",
        "text-start",
        "text-delta",
        "text-end",
        "finish",
    ]
    assert events[-1] is None

    deltas = [e["delta"] for e in events if e and e["type"] == "text-delta"]
    assert "".join(deltas) == "plan reply"

    assert lock.locked() is False
    assert session.committed is True
    assert len(session._added) == 1
    assert session._added[0].role == "assistant"
    assert session._added[0].content == "plan reply"
    assert session._added[0].is_error is False
    assert session._added[0].meta == {"phase": "plan"}