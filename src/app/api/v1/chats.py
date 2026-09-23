import datetime as dt

from fastapi import APIRouter, Depends, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_current_user, get_owned_chat, paginate
from app.models import Chat, User
from app.schemas.chat import ChatCreate, ChatOut, ChatUpdate
from app.schemas.common import PageParams, Paginated

router = APIRouter()


@router.post("", response_model=ChatOut, status_code=status.HTTP_201_CREATED)
async def create_chat(
    payload: ChatCreate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> Chat:
    chat = Chat(user_id=current_user.id)
    if payload.title is not None:
        chat.title = payload.title
    if payload.model is not None:
        chat.model = payload.model
    session.add(chat)
    await session.commit()
    await session.refresh(chat)
    return chat


@router.get("", response_model=Paginated[ChatOut])
async def list_chats(
    params: PageParams = Depends(paginate),
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> Paginated[ChatOut]:
    base = select(Chat).where(
        Chat.user_id == current_user.id, Chat.deleted_at.is_(None)
    )
    total = (
        await session.execute(
            select(func.count()).select_from(Chat).where(
                Chat.user_id == current_user.id, Chat.deleted_at.is_(None)
            )
        )
    ).scalar_one()
    rows = (
        await session.execute(
            base.order_by(Chat.created_at.desc())
            .offset((params.page - 1) * params.page_size)
            .limit(params.page_size)
        )
    ).scalars().all()
    return Paginated[ChatOut](
        items=[ChatOut.model_validate(chat) for chat in rows],
        page=params.page,
        page_size=params.page_size,
        total=total,
    )


@router.get("/{chat_id}", response_model=ChatOut)
async def get_chat(chat: Chat = Depends(get_owned_chat)) -> Chat:
    return chat


@router.patch("/{chat_id}", response_model=ChatOut)
async def rename_chat(
    payload: ChatUpdate,
    chat: Chat = Depends(get_owned_chat),
    session: AsyncSession = Depends(get_session),
) -> Chat:
    chat.title = payload.title
    await session.commit()
    await session.refresh(chat)
    return chat


@router.delete("/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def soft_delete_chat(
    chat: Chat = Depends(get_owned_chat),
    session: AsyncSession = Depends(get_session),
) -> None:
    chat.deleted_at = dt.datetime.now(dt.UTC)
    await session.commit()