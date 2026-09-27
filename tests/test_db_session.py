"""Tests for request-scoped session teardown in app/db.py.

Covers the mid-stream teardown crash where a connection that died under a
long streaming response made session.close() raise on rollback.
"""

import pytest
from sqlalchemy.exc import InterfaceError

from app import db


def _install_factory(monkeypatch, session_type, produced):
    monkeypatch.setattr(db, "get_session_factory", lambda: session_type)
    return session_type


async def test_get_session_closes_normally(monkeypatch):
    produced = []

    class _Session:
        def __init__(self):
            self.closed = False
            produced.append(self)

        async def close(self):
            self.closed = True

    _install_factory(monkeypatch, _Session, produced)

    seen = []
    async for session in db.get_session():
        seen.append(session)

    assert len(seen) == 1
    assert produced[0].closed is True


async def test_get_session_tolerates_dead_connection(monkeypatch):
    produced = []

    class _Session:
        def __init__(self):
            self.closed = False
            produced.append(self)

        async def close(self):
            self.closed = True
            raise InterfaceError(
                "cannot call Transaction.rollback(): the underlying connection is closed",
                "ROLLBACK",
                "08P01",
            )

    _install_factory(monkeypatch, _Session, produced)

    seen = []
    async for session in db.get_session():
        seen.append(session)

    assert len(seen) == 1
    assert produced[0].closed is True


async def test_get_session_closes_even_when_consumer_raises(monkeypatch):
    produced = []

    class _Session:
        def __init__(self):
            self.closed = False
            produced.append(self)

        async def close(self):
            self.closed = True

    _install_factory(monkeypatch, _Session, produced)

    agen = db.get_session()
    await anext(agen)
    assert len(produced) == 1
    with pytest.raises(RuntimeError, match="simulated handler failure"):
        await agen.athrow(RuntimeError("simulated handler failure"))
    assert produced[0].closed is True