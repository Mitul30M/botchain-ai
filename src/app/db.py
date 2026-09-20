from collections.abc import AsyncIterator

from sqlalchemy import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_ASYNC_PG_DROP_KEYS = {"sslmode", "channel_binding", "uselibpqcompat"}

_engine = None
_session_factory = None


def postgres_uri(driver: str) -> str:
    raw = get_settings().database_url
    if not raw:
        return raw
    url = make_url(raw)
    query: dict[str, str] = {}
    for key, value in url.query.items():
        if driver == "asyncpg" and key in _ASYNC_PG_DROP_KEYS:
            continue
        if driver == "psycopg" and key == "uselibpqcompat":
            continue
        query[key] = value
    url = url.set(drivername=f"postgresql+{driver}", query=query)
    return url.render_as_string(hide_password=False)


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            postgres_uri("asyncpg"),
            connect_args={"ssl": True},
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), class_=AsyncSession, expire_on_commit=False)
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session