from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_current_user
from app.models import User
from app.services.account import purge_user_data

router = APIRouter()


@router.delete("/data", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me_data(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> None:
    """Delete the current user's data (chats, messages, attachments, billing).

    Never deletes the ``users`` row — the frontend's Kinde callbacks own the
    user lifecycle. Idempotent: a second call with nothing left returns 204.
    """
    await purge_user_data(request, session, current_user)