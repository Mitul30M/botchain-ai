"""Chat lifecycle: rename validation (strip, 120 cap, updated_at bump) and the
soft-delete 409 guard against an active run."""

import datetime as dt

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import chats as chats_module
from app.api.v1.chats import router as chats_router
from app.db import get_session
from app.deps import get_owned_chat


class _ChatChat:
    def __init__(self, chat_id, title="Old title"):
        self.id = chat_id
        self.title = title
        self.model = "claude-sonnet-4-6"
        self.pinned = False
        self.archived = False
        self.meta = {}
        self.created_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        self.updated_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        self.deleted_at = None


class _ChatSession:
    def __init__(self, order=None):
        self.order = order if order is not None else []
        self.commits = 0

    async def commit(self):
        self.commits += 1
        self.order.append("commit")

    async def refresh(self, row):
        pass

    async def close(self):
        pass


class _LockedLock:
    def locked(self):
        return True


def _make_app(session, chat):
    app = FastAPI()
    app.include_router(chats_router, prefix="/chats")
    app.dependency_overrides[get_owned_chat] = lambda: chat
    app.dependency_overrides[get_session] = lambda: session
    return app


def test_rename_strips_and_bumps_updated_at():
    session = _ChatSession()
    chat = _ChatChat("c-rename-1", title="Old")
    app = _make_app(session, chat)

    with TestClient(app) as client:
        response = client.patch("/chats/c-rename-1", json={"title": "  New title  "})

    assert response.status_code == 200
    assert chat.title == "New title"
    assert chat.updated_at > dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    assert chat.updated_at.tzinfo is not None
    assert session.commits == 1


def test_rename_blank_title_rejected():
    app = _make_app(_ChatSession(), _ChatChat("c-rename-2"))

    with TestClient(app) as client:
        response = client.patch("/chats/c-rename-2", json={"title": "   "})

    assert response.status_code == 422


def test_rename_empty_title_rejected():
    app = _make_app(_ChatSession(), _ChatChat("c-rename-3"))

    with TestClient(app) as client:
        response = client.patch("/chats/c-rename-3", json={"title": ""})

    assert response.status_code == 422


def test_rename_missing_title_rejected():
    app = _make_app(_ChatSession(), _ChatChat("c-rename-4"))

    with TestClient(app) as client:
        response = client.patch("/chats/c-rename-4", json={})

    assert response.status_code == 422


def test_rename_title_over_120_chars_rejected():
    app = _make_app(_ChatSession(), _ChatChat("c-rename-5"))

    with TestClient(app) as client:
        response = client.patch("/chats/c-rename-5", json={"title": "a" * 121})

    assert response.status_code == 422


def test_delete_chat_sets_deleted_at():
    session = _ChatSession()
    chat = _ChatChat("c-del-1")
    app = _make_app(session, chat)

    with TestClient(app) as client:
        response = client.delete("/chats/c-del-1")

    assert response.status_code == 204
    assert chat.deleted_at is not None
    assert session.commits == 1


def test_delete_chat_409_when_run_active(monkeypatch):
    session = _ChatSession()
    chat = _ChatChat("c-del-2")
    app = _make_app(session, chat)
    monkeypatch.setattr(chats_module, "get_chat_lock", lambda _: _LockedLock())

    with TestClient(app) as client:
        response = client.delete("/chats/c-del-2")

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "A response is still being generated for this chat — try again in a moment."
    )
    assert chat.deleted_at is None
    assert session.commits == 0