from __future__ import annotations

from dataclasses import dataclass

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.db import psycopg_conninfo

_CHECKPOINTER_KWARGS = {
    "autocommit": True,
    "row_factory": dict_row,
    "prepare_threshold": 0,
}


@dataclass
class CheckpointService:
    """Wraps an AsyncPostgresSaver and its backing connection pool.

    The pool is created by setup_checkpoint() and closed via close().
    """

    checkpointer: AsyncPostgresSaver
    _pool: AsyncConnectionPool

    async def close(self) -> None:
        """Close the underlying connection pool."""
        await self._pool.close()


async def setup_checkpoint(max_size: int = 8) -> CheckpointService:
    """Create and initialise the LangGraph Postgres checkpointer.

    Opens a psycopg_pool.AsyncConnectionPool against the configured
    DATABASE_URL, wraps it in an AsyncPostgresSaver, and runs
    checkpointer.setup() to create the LangGraph-owned tables
    (checkpoints, checkpoint_blobs, checkpoint_writes,
    checkpoint_migrations) if they don't already exist.

    The returned CheckpointService holds both the checkpointer (for
    LangGraph) and the pool (for lifecycle management). Call
    service.close() on application shutdown.
    """
    pool = AsyncConnectionPool(
        conninfo=psycopg_conninfo(),
        min_size=0,
        max_size=max_size,
        kwargs=_CHECKPOINTER_KWARGS,
        open=False,
    )
    await pool.open()
    checkpointer = AsyncPostgresSaver(pool)
    await checkpointer.setup()
    return CheckpointService(checkpointer=checkpointer, _pool=pool)
