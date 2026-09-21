from collections.abc import AsyncIterator

from sqlalchemy import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_ASYNC_PG_DROP_KEYS = {"sslmode", "channel_binding", "uselibpqcompat"}

_PSYCOPG_DROP_KEYS = {"uselibpqcompat"}

_engine = None
_session_factory = None


def postgres_uri(driver: str) -> str:
    """Rewrite the configured DATABASE_URL for the given SQLAlchemy driver.

    asyncpg cannot accept Neon's libpq-only query params (sslmode,
    channel_binding, uselibpqcompat), so all three are stripped.
    psycopg/libpq understands sslmode and channel_binding natively;
    only uselibpqcompat (a Neon-specific flag) is dropped.
    """
    raw = get_settings().database_url
    if not raw:
        return raw
    url = make_url(raw)
    drop_keys = _PSYCOPG_DROP_KEYS if driver == "psycopg" else _ASYNC_PG_DROP_KEYS
    query: dict[str, str] = {}
    for key, value in url.query.items():
        if key in drop_keys:
            continue
        query[key] = value
    url = url.set(drivername=f"postgresql+{driver}", query=query)
    return url.render_as_string(hide_password=False)


def psycopg_conninfo() -> str:
    """Return a plain ``postgresql://`` conninfo string for psycopg3.

    Strips the ``+psycopg`` dialect suffix that postgres_uri() adds for
    SQLAlchemy, since psycopg3's native connect does not accept it.
    """
    return postgres_uri("psycopg").replace("postgresql+psycopg://", "postgresql://", 1)


def get_engine():
    """Return the process-wide async engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            postgres_uri("asyncpg"),
            connect_args={"ssl": True},
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide async session factory, creating it lazily."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), class_=AsyncSession, expire_on_commit=False)
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession for the lifetime of one request dependency."""
    async with get_session_factory()() as session:
        yield session