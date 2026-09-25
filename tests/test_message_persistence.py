"""Unit tests for the Persistence helpers in api/v1/messages.py."""

from langchain_core.messages import AIMessage, HumanMessage

from app.api.v1.messages import _final_assistant_text, _flush_assistant_row


class _FakeState:
    def __init__(self, values):
        self.values = values


class _FakeAgent:
    def __init__(self, messages):
        self._messages = messages

    async def aget_state(self, config):
        return _FakeState({"messages": self._messages})


async def test_final_assistant_text_returns_last_ai_message():
    agent = _FakeAgent(
        [
            HumanMessage(content="hi"),
            AIMessage(content="plan reply"),
            AIMessage(content="ready"),
        ]
    )
    assert await _final_assistant_text(agent, {}) == "ready"


async def test_final_assistant_text_skips_non_text_blocks():
    agent = _FakeAgent(
        [
            AIMessage(
                content=[
                    {"type": "text", "text": "hello "},
                    {"type": "tool_use", "name": "f", "input": {}},
                ]
            )
        ]
    )
    assert await _final_assistant_text(agent, {}) == "hello "


async def test_final_assistant_text_empty_when_no_messages():
    agent = _FakeAgent([])
    assert await _final_assistant_text(agent, {}) == ""


async def test_final_assistant_text_empty_when_state_none():
    agent = _FakeAgent([])

    async def _none(config):
        return None

    agent.aget_state = _none
    assert await _final_assistant_text(agent, {}) == ""


class _FakeChat:
    def __init__(self, chat_id, context_summary=None):
        self.id = chat_id
        self.context_summary = context_summary


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeSession:
    def __init__(self, chat):
        self._chat = chat
        self._added = []
        self.committed = False
        self.queried = False

    async def execute(self, stmt):
        self.queried = True
        return _FakeResult(self._chat)

    def add(self, message):
        self._added.append(message)

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_flush_assistant_row_confirm_sets_context_summary(monkeypatch):
    chat = _FakeChat("chat-1")
    session = _FakeSession(chat)
    monkeypatch.setattr("app.api.v1.messages.get_session_factory", lambda: lambda: session)

    await _flush_assistant_row(
        "chat-1",
        "pending summary",
        False,
        meta={"phase": "confirm"},
        context_summary="pending summary",
    )

    assert session.queried is True
    assert chat.context_summary == "pending summary"
    assert session.committed is True
    assert session._added[0].role == "assistant"
    assert session._added[0].content == "pending summary"


async def test_flush_assistant_row_without_summary_does_not_query_chat(monkeypatch):
    session = _FakeSession(None)
    monkeypatch.setattr("app.api.v1.messages.get_session_factory", lambda: lambda: session)

    await _flush_assistant_row("chat-1", "hello", False, meta={"phase": "plan"})

    assert session.queried is False
    assert session.committed is True
    assert session._added[0].role == "assistant"