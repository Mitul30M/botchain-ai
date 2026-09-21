import os
import subprocess
from pathlib import Path

import pytest
from langgraph.checkpoint.base import empty_checkpoint

from app.services.checkpoint import CheckpointService, setup_checkpoint

_LANGGRAPH_TABLES = {
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
}


@pytest.fixture(scope="module")
async def ckpt_service():
    """Set up the checkpointer once for the module, tear down after."""
    svc = await setup_checkpoint()
    yield svc
    from psycopg import AsyncConnection

    async with await AsyncConnection.connect(svc._pool.conninfo, autocommit=True) as conn:
        for table in _LANGGRAPH_TABLES:
            await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    await svc.close()


async def _table_exists(conn, table_name: str) -> bool:
    """Check whether a table exists in the public schema."""
    result = await conn.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s)",
        (table_name,),
    )
    row = await result.fetchone()
    return row[0]


async def test_checkpoint_setup_creates_tables(ckpt_service: CheckpointService):
    """setup() must create all 4 LangGraph-owned tables."""
    from psycopg import AsyncConnection

    async with await AsyncConnection.connect(ckpt_service._pool.conninfo, autocommit=True) as conn:
        for table in _LANGGRAPH_TABLES:
            assert await _table_exists(conn, table), f"{table} not created"


async def test_checkpoint_round_trip(ckpt_service: CheckpointService):
    """A checkpoint written via aput() must be readable back via aget()."""
    config = {"configurable": {"thread_id": "test-round-trip", "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    metadata = {"source": "test"}

    await ckpt_service.checkpointer.aput(config, checkpoint, metadata, {})

    result = await ckpt_service.checkpointer.aget(config)
    assert result is not None
    assert result["id"] == checkpoint["id"]


def test_alembic_excludes_checkpoint_tables(ckpt_service: CheckpointService):
    """alembic revision --autogenerate must not see LangGraph tables."""
    project_root = Path(__file__).resolve().parent.parent
    revision_file = None

    try:
        subprocess.run(
            [
                "uv", "run", "alembic", "revision",
                "--autogenerate",
                "-m", "verify-checkpoint-exclusion",
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=30,
        )

        versions_dir = project_root / "alembic" / "versions"
        revision_files = sorted(versions_dir.glob("*.py"), key=os.path.getmtime)
        if revision_files:
            revision_file = revision_files[-1]
            content = revision_file.read_text()

            has_upgrade = "def upgrade()" in content

            if has_upgrade and "pass" not in content.split("def upgrade()")[1].split("def downgrade()")[0]:
                for table in _LANGGRAPH_TABLES:
                    assert table not in content, (
                        f"autogenerate proposed changes for LangGraph table {table}"
                    )
    finally:
        if revision_file and revision_file.exists():
            revision_file.unlink()
