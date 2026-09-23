from fastapi import Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_token
from app.db import get_session
from app.models import Chat, User
from app.schemas.common import PageParams


def _bearer_token(request: Request) -> str:
    """Extract the raw JWT from an `Authorization: Bearer ...` header.

    Raises 401 if the header is missing or not a well-formed bearer scheme.
    """
    auth = request.headers.get("Authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token.strip()


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User:
    """Resolve the verified token's `sub` claim to the local User row.

    Read-only: never creates or mutates User rows. Raises 401 if the token is
    invalid or no User exists for the kinde_id (registration incomplete).
    Results are cached on request.state so a handler only queries once.
    """
    cached = getattr(request.state, "current_user", None)
    if cached is not None:
        return cached

    token = _bearer_token(request)
    claims = await verify_token(token)

    kinde_id = claims.get("sub")
    if not kinde_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = (
        await session.execute(select(User).where(User.kinde_id == kinde_id))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Registration not complete",
            headers={"WWW-Authenticate": "Bearer"},
        )

    request.state.current_user = user
    return user


async def paginate(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> PageParams:
    """Parse common `page`/`page_size` query params into a shared PageParams."""
    return PageParams(page=page, page_size=page_size)


async def get_owned_chat(
    chat_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> Chat:
    """Resolve a non-soft-deleted chat owned by the current user.

    Returns 404 for a missing chat, a soft-deleted chat, or a chat that belongs
    to someone else — the handler never learns that another user's chat exists.
    """
    chat = (
        await session.execute(
            select(Chat).where(Chat.id == chat_id, Chat.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if chat is None or chat.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat not found",
        )
    return chat