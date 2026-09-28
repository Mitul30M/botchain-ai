"""Account purge (DELETE /me/data): one transaction wiping chats, messages,
attachments, and billing rows — never ``users`` — after LangGraph threads are
removed, with a 409 when any chat has an active run."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Delete, Select

from app.api.v1.me import router as me_router
from app.db import get_session
from app.deps import get_current_user
from app.services import chat_locks
from app.services.account import ACTIVE_RUN_DETAIL
from app.services.chat_locks import drop_chat_lock


class _FakeUser:
    id = "user-1"
    kinde_id = "kp_test"


class _FakeLock:
    def __init__(self, locked=False):
        self._locked = locked
        self.released = 0

    def locked(self):
        return self._locked

    async def acquire(self):
        self._locked = True

    def release(self):
        self._locked = False
        self.released += 1


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _SelectResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


def _delete_table(stmt):
    return str(stmt.compile()).split("DELETE FROM ", 1)[1].split(" ")[0]


class _PurgeSession:
    def __init__(self, chat_ids, order):
        self._chat_ids = chat_ids
        self.order = order
        self.commits = 0

    async def execute(self, stmt):
        if isinstance(stmt, Select):
            return _SelectResult(self._chat_ids)
        if isinstance(stmt, Delete):
            self.order.append(f"delete:{_delete_table(stmt)}")
            return SimpleNamespace()
        raise AssertionError(f"unhandled statement: {stmt!r}")

    async def commit(self):
        self.commits += 1
        self.order.append("commit")

    async def close(self):
        pass


class _FakeCheckpointer:
    def __init__(self, order):
        self.order = order
        self.deleted = []

    async def adelete_thread(self, thread_id):
        self.order.append(f"delete-thread:{thread_id}")
        self.deleted.append(thread_id)


def _make_app(
    session, checkpointer, *, override_auth: bool = True
):
    app = FastAPI()
    app.state.checkpointer = checkpointer
    app.include_router(me_router, prefix="/me")
    app.dependency_overrides[get_session] = lambda: session
    if override_auth:
        app.dependency_overrides[get_current_user] = lambda: _FakeUser()
    return app


def _release_all(chat_ids):
    for chat_id in chat_ids:
        drop_chat_lock(chat_id)


def test_purge_deletes_all_user_data():
    order = []
    session = _PurgeSession(["purge-1", "purge-2", "purge-3"], order)
    checkpointer = _FakeCheckpointer(order)
    app = _make_app(session, checkpointer)

    with TestClient(app) as client:
        response = client.delete("/me/data")

    assert response.status_code == 204
    assert checkpointer.deleted == ["purge-1", "purge-2", "purge-3"]
    thread_pos = {id: order.index(f"delete-thread:{id}") for id in checkpointer.deleted}
    assert order.index("delete:attachments") > max(thread_pos.values())
    sql_order = [event for event in order if event.startswith("delete:") or event == "commit"]
    assert sql_order == [
        "delete:attachments",
        "delete:messages",
        "delete:chats",
        "delete:credit_transactions",
        "delete:payment_topups",
        "delete:credit_wallets",
        "commit",
    ]
    assert session.commits == 1
    for chat_id in ("purge-1", "purge-2", "purge-3"):
        assert chat_id not in chat_locks._chat_locks


def test_purge_409_releases_taken_locks(monkeypatch):
    taker = _FakeLock(locked=True)
    taken = _FakeLock()

    def fake_get_lock(chat_id):
        return taker if chat_id == "purge-2" else taken

    monkeypatch.setattr("app.services.account.get_chat_lock", fake_get_lock)
    order = []
    session = _PurgeSession(["purge-1", "purge-2", "purge-3"], order)
    checkpointer = _FakeCheckpointer(order)
    app = _make_app(session, checkpointer)

    with TestClient(app) as client:
        response = client.delete("/me/data")

    try:
        assert response.status_code == 409
        assert response.json()["detail"] == ACTIVE_RUN_DETAIL
        assert checkpointer.deleted == []
        assert session.commits == 0
        assert taken.released == 1
        assert taker.released == 0
        assert taker.locked()
    finally:
        _release_all(["purge-1", "purge-2", "purge-3"])


def test_purge_idempotent_when_no_chats():
    order = []
    session = _PurgeSession([], order)
    checkpointer = _FakeCheckpointer(order)
    app = _make_app(session, checkpointer)

    with TestClient(app) as client:
        response = client.delete("/me/data")

    assert response.status_code == 204
    assert checkpointer.deleted == []
    assert session.commits == 1


def test_purge_requires_auth():
    order = []
    session = _PurgeSession(["purge-9"], order)
    app = _make_app(session, _FakeCheckpointer(order), override_auth=False)

    with TestClient(app) as client:
        response = client.delete("/me/data")

    assert response.status_code == 401
    assert response.json()["detail"] == "Not authenticated"
    _release_all(["purge-9"])