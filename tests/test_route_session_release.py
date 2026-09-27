"""Route-level checks that streaming handlers release the request session first.

Regression for the mid-stream teardown crash: the request-scoped session used
to stay open (with an in-flight transaction) across the whole streaming
response, and its close-at-teardown exploded when the connection had died
under the stream. Handlers must close the session before returning the
StreamingResponse, and the stream must still work after that close.
"""

import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.api.v1.messages import router as messages_router
from app.db import get_session
from app.deps import get_owned_chat
from app.models import Message


def _parse_body(body: str) -> list:
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            payload = line[len("data: ") :].strip()
            events.append(None if payload == "[DONE]" else json.loads(payload))
    return events


class _RouteState:
    def __init__(self, values):
        self.values = values
        self.tasks = []


class _RouteAgent:
    def __init__(self, values, events, order):
        self._values = values
        self._events = events
        self._order = order

    async def astream(self, input, config, stream_mode):
        self._order.append("stream")
        for event in self._events:
            yield event

    async def aget_state(self, config):
        return _RouteState(self._values)


class _RouteChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _RouteScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _RouteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return _RouteScalars(self._rows)


class _DepSession:
    def __init__(self, rows, order):
        self._rows = rows
        self._order = order
        self._added = []
        self.closed = False

    async def execute(self, stmt):
        return _RouteResult(self._rows)

    def add(self, row):
        self._added.append(row)

    async def flush(self):
        for row in self._added:
            if getattr(row, "id", None) is None:
                row.id = f"m-{self._added.index(row) + 1}"

    async def commit(self):
        pass

    async def refresh(self, row):
        pass

    async def close(self):
        if not self.closed:
            self._order.append("session-close")
            self.closed = True


class _FlushSession(_DepSession):
    async def commit(self):
        self._added.clear()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _make_app(agent, session, chat):
    app = FastAPI()
    app.state.agent = agent
    app.include_router(messages_router)
    app.dependency_overrides[get_owned_chat] = lambda: chat
    app.dependency_overrides[get_session] = lambda: session
    return app


def test_send_message_closes_session_before_streaming(monkeypatch):
    order = []
    session = _DepSession([], order)
    monkeypatch.setattr(
        "app.api.v1.messages.get_session_factory", lambda: lambda: _FlushSession([], order)
    )
    agent = _RouteAgent(
        values={"messages": [AIMessage(content="plan reply")], "phase": "plan"},
        events=[
            ("custom", {"status": "Planning"}),
            ("messages", (AIMessage(content="plan reply"), {})),
        ],
        order=order,
    )
    app = _make_app(agent, session, _RouteChat("chat-1"))

    with TestClient(app) as client:
        response = client.post("/chat-1/messages", json={"content": "hi"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert order == ["session-close", "stream"]

    events = _parse_body(response.text)
    types = [None if e is None else e["type"] for e in events]
    assert types == [
        "start",
        "data-status",
        "text-start",
        "text-delta",
        "text-end",
        "finish",
        None,
    ]
    deltas = [e["delta"] for e in events if e and e["type"] == "text-delta"]
    assert "".join(deltas) == "plan reply"


def test_approve_closes_session_before_streaming(monkeypatch):
    pending = Message(
        id="m-pending",
        chat_id="chat-1",
        role="assistant",
        content="build?",
        meta={"phase": "confirm", "approval": {"status": "pending"}},
    )
    order = []
    session = _DepSession([pending], order)
    monkeypatch.setattr(
        "app.api.v1.messages.get_session_factory", lambda: lambda: _FlushSession([], order)
    )
    agent = _RouteAgent(
        values={
            "messages": [AIMessage(content="workflow ready")],
            "phase": "done",
            "validation_status": "valid",
            "workflow_json": {"name": "Webhook and Slack"},
        },
        events=[
            ("custom", {"status": "Building"}),
            ("messages", (AIMessage(content="workflow ready"), {})),
        ],
        order=order,
    )
    app = _make_app(agent, session, _RouteChat("chat-1"))

    with TestClient(app) as client:
        response = client.post("/chat-1/approve", json={"approved": True})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert order == ["session-close", "stream"]

    events = _parse_body(response.text)
    types = [None if e is None else e["type"] for e in events]
    assert types == [
        "start",
        "data-status",
        "text-start",
        "text-delta",
        "text-end",
        "finish",
        None,
    ]
    assert not any(e and e.get("type") == "error" for e in events)


def test_send_message_conflict_409_with_pending_approval(monkeypatch):
    class _PendingAgent:
        async def aget_state(self, config):
            return SimpleNamespace(
                values={"phase": "confirm"},
                tasks=[SimpleNamespace(interrupts=[("continue", None)])],
            )

    session = _DepSession([], [])
    app = FastAPI()
    app.state.agent = _PendingAgent()
    app.include_router(messages_router)
    app.dependency_overrides[get_owned_chat] = lambda: _RouteChat("chat-2")
    app.dependency_overrides[get_session] = lambda: session

    with TestClient(app) as client:
        response = client.post("/chat-2/messages", json={"content": "hi"})

    assert response.status_code == 409
    assert "approval" in response.json()["detail"]
    assert session.closed is False


def test_approve_no_pending_409_releases_lock_once(monkeypatch):
    session = _DepSession([], [])

    class _NoopAgent:
        async def astream(self, input, config, stream_mode):
            yield ()
            raise AssertionError("stream must not start for the 409 path")

    app = FastAPI()
    app.state.agent = _NoopAgent()
    app.include_router(messages_router)
    app.dependency_overrides[get_owned_chat] = lambda: _RouteChat("chat-3")
    app.dependency_overrides[get_session] = lambda: session

    with TestClient(app) as client:
        response = client.post("/chat-3/approve", json={"approved": True})

    assert response.status_code == 409
    assert response.json()["detail"] == "No pending approval for this chat"