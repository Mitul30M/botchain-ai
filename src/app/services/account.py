import asyncio

from fastapi import HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Attachment,
    Chat,
    CreditTransaction,
    CreditWallet,
    Message,
    PaymentTopup,
    User,
)
from app.services.chat_locks import drop_chat_lock, get_chat_lock

ACTIVE_RUN_DETAIL = (
    "A request is still being processed for this account — try again in a moment."
)


async def purge_user_data(
    request: Request,
    session: AsyncSession,
    current_user: User,
) -> None:
    """Delete every row except ``users`` that belongs to the current user.

    Chat id rows are the durable record of LangGraph thread ids, so checkpoint
    threads are deleted before their chat rows. All SQL runs as one transaction;
    the user row, which the backend never creates or mutates, is never touched.
    """
    chat_ids = list(
        (
            await session.execute(
                select(Chat.id).where(Chat.user_id == current_user.id)
            )
        ).scalars().all()
    )

    held: list[asyncio.Lock] = []
    for chat_id in chat_ids:
        lock = get_chat_lock(chat_id)
        if lock.locked():
            for taken in held:
                taken.release()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=ACTIVE_RUN_DETAIL,
            )
        await lock.acquire()
        held.append(lock)

    try:
        checkpointer = request.app.state.checkpointer
        for chat_id in chat_ids:
            await checkpointer.adelete_thread(chat_id)

        chat_sub = select(Chat.id).where(Chat.user_id == current_user.id)
        message_sub = select(Message.id).where(Message.chat_id.in_(chat_sub))
        await session.execute(
            delete(Attachment).where(Attachment.message_id.in_(message_sub))
        )
        await session.execute(delete(Message).where(Message.chat_id.in_(chat_sub)))
        await session.execute(delete(Chat).where(Chat.user_id == current_user.id))
        await session.execute(
            delete(CreditTransaction).where(
                CreditTransaction.user_id == current_user.id
            )
        )
        await session.execute(
            delete(PaymentTopup).where(PaymentTopup.user_id == current_user.id)
        )
        await session.execute(
            delete(CreditWallet).where(CreditWallet.user_id == current_user.id)
        )
        await session.commit()
    finally:
        for lock in held:
            lock.release()
        for chat_id in chat_ids:
            drop_chat_lock(chat_id)