import asyncio
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session, get_session_factory
from app.deps import get_owned_chat, paginate
from app.models import Chat, Message
from app.schemas.common import PageParams, Paginated
from app.schemas.message import ApproveRequest, MessageCreate, MessageOut

router = APIRouter()

logger = logging.getLogger(__name__)

APPROVAL_PENDING = "pending"

_STUB_CHUNK_DELAY = 0.02

_background_tasks: set[asyncio.Task] = set()


async def _stub_chunks(content: str) -> AsyncIterator[str]:
    """Stub agent stream: echo the user's message in small incremental chunks.

    TODO Phase 5: replace with the real LangGraph agent stream via app.state.agent.
    """
    yield "[echo] "
    words = content.split()
    batch: list[str] = []
    for word in words:
        batch.append(word)
        if len(batch) == 3:
            yield " ".join(batch) + " "
            batch = []
            await asyncio.sleep(_STUB_CHUNK_DELAY)
    if batch:
        yield " ".join(batch)


def _serialize_response(message: Message) -> MessageOut:
    return MessageOut.model_validate(message)


async def _flush_assistant_row(chat_id: str, content: str, is_error: bool) -> None:
    async with get_session_factory()() as session:
        session.add(
            Message(chat_id=chat_id, role="assistant", content=content, is_error=is_error)
        )
        await session.commit()


def _forget_task(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    if exc := task.exception():
        logger.error("background assistant flush failed", exc_info=exc)


def _fire_and_forget(coro) -> None:
    """Run a coroutine on a background task with a strong reference.

    asyncio keeps only weak references to tasks, so a bare
    ``asyncio.create_task`` can be garbage-collected mid-flight once the
    generator that spawned it is gone. Holding the task in a module-level set
    keeps it alive, and the done-callback releases it and surfaces any failure.
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_forget_task)


async def _assistant_stream(chat_id: str, content: str) -> AsyncIterator[str]:
    """Forward stub chunks to the client while buffering the full reply.

    Persists exactly one assistant Message row once the stream finishes (or is
    aborted), with is_error reflecting whether it completed cleanly. The request
    scoped session from Depends(get_session) may be torn down when the handler's
    response returns, so this generator opens its own session for the insert.
    On client disconnect uvicorn cancels the request task, so a finally-block
    await would be re-cancelled before committing; in that case the write is
    handed to a strongly-referenced background task that survives the request
    task's cancellation.
    """
    buffer: list[str] = []
    is_error = False
    request_task = asyncio.current_task()
    try:
        async for chunk in _stub_chunks(content):
            buffer.append(chunk)
            yield chunk
    except BaseException:
        is_error = True
        raise
    finally:
        reply = "".join(buffer)
        if request_task is not None and request_task.cancelling():
            _fire_and_forget(_flush_assistant_row(chat_id, reply, is_error=True))
        else:
            await _flush_assistant_row(chat_id, reply, is_error)


@router.get("/{chat_id}/messages", response_model=Paginated[MessageOut])
async def list_messages(
    chat: Chat = Depends(get_owned_chat),
    params: PageParams = Depends(paginate),
    session: AsyncSession = Depends(get_session),
) -> Paginated[MessageOut]:
    where = Message.chat_id == chat.id
    total = (
        await session.execute(select(func.count()).select_from(Message).where(where))
    ).scalar_one()
    rows = (
        await session.execute(
            select(Message)
            .where(where)
            .order_by(Message.created_at.asc(), Message.id.asc())
            .offset((params.page - 1) * params.page_size)
            .limit(params.page_size)
        )
    ).scalars().all()
    return Paginated[MessageOut](
        items=[_serialize_response(message) for message in rows],
        page=params.page,
        page_size=params.page_size,
        total=total,
    )


@router.post("/{chat_id}/messages")
async def send_message(
    payload: MessageCreate,
    chat: Chat = Depends(get_owned_chat),
    session: AsyncSession = Depends(get_session),
):
    if payload.parent_id is not None:
        parent = (
            await session.execute(
                select(Message).where(Message.id == payload.parent_id)
            )
        ).scalar_one_or_none()
        if parent is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Parent message not found",
            )
        if parent.chat_id != chat.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Parent message does not belong to this chat",
            )

    session.add(
        Message(chat_id=chat.id, role="user", content=payload.content, parent_id=payload.parent_id)
    )
    await session.commit()

    return StreamingResponse(
        _assistant_stream(chat.id, payload.content),
        media_type="text/plain; charset=utf-8",
    )


@router.post("/{chat_id}/approve", response_model=MessageOut)
async def approve(
    payload: ApproveRequest,
    chat: Chat = Depends(get_owned_chat),
    session: AsyncSession = Depends(get_session),
) -> MessageOut:
    """Apply the approval/rejection decision for a pending confirmation message.

    TODO Phase 5: resume the interrupted LangGraph run via app.state.checkpointer
    once a real confirm_node can pause. Phase 4 only persists the decision.
    """
    recent = (
        await session.execute(
            select(Message)
            .where(Message.chat_id == chat.id, Message.role == "assistant")
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(10)
        )
    ).scalars().all()

    pending = next(
        (message for message in recent if message.meta.get("approval", {}).get("status") == APPROVAL_PENDING),
        None,
    )
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No pending approval for this chat",
        )

    meta = dict(pending.meta)
    meta["approval"] = {
        "status": "approved" if payload.approved else "rejected",
        "feedback": payload.feedback,
    }
    pending.meta = meta
    await session.commit()
    await session.refresh(pending)
    return _serialize_response(pending)