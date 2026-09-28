import asyncio

_chat_locks: dict[str, asyncio.Lock] = {}


def get_chat_lock(chat_id: str) -> asyncio.Lock:
    """Return the per-chat asyncio lock, creating it on first use.

    Serialises sends and approval-resumes for a single chat so two requests
    cannot interleave LangGraph runs against the same thread. Chat delete and
    the account purge read ``.locked()`` to detect an active run. No eviction
    outside of a successful purge: this is a single-replica service, so the
    registry stays small for the chat count the app is realistically going
    to see.
    """
    return _chat_locks.setdefault(chat_id, asyncio.Lock())


def drop_chat_lock(chat_id: str) -> None:
    """Remove the registry entry for a chat (used after a successful purge)."""
    _chat_locks.pop(chat_id, None)