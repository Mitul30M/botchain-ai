import asyncio
import logging
from collections.abc import AsyncIterator, Mapping

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command
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

_background_tasks: set[asyncio.Task] = set()

_chat_locks: dict[str, asyncio.Lock] = {}


def _chat_lock(chat_id: str) -> asyncio.Lock:
    """Return the per-chat asyncio lock, creating it on first use.

    Serialises sends and approval-resumes for a single chat so two requests
    cannot interleave LangGraph runs against the same thread. No eviction:
    this is a single-replica service, so the registry stays small for the
    chat count the app is realistically going to see.
    """
    return _chat_locks.setdefault(chat_id, asyncio.Lock())


def _serialize_response(message: Message) -> MessageOut:
    return MessageOut.model_validate(message)


def _content_text(message: AIMessage) -> str:
    """Extract plain text from an AIMessage/AIMessageChunk content."""
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, Mapping) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def _status_text(payload: object) -> str:
    """Extract a plain-string status from a custom stream payload."""
    if isinstance(payload, Mapping):
        return str(payload.get("status", ""))
    return str(payload)


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


async def _has_pending_approval(agent, config: dict) -> bool:
    """Return True if the thread is parked at an unresolved approval interrupt."""
    state = await agent.aget_state(config)
    if state is None or not state.tasks:
        return False
    return any(getattr(task, "interrupts", ()) for task in state.tasks)


async def _flush_assistant_row(
    chat_id: str,
    content: str,
    is_error: bool,
    meta: dict | None = None,
    context_summary: str | None = None,
) -> None:
    """Persist one assistant Message row (open its own session; commit).

    The request-scoped session from Depends(get_session) may be torn down when
    the handler's response returns, so this generator opens a fresh session for
    the write. When meta signals a pending approval the chat row's
    context_summary is set to the confirm summary if it isn't already present.
    """
    async with get_session_factory()() as session:
        if context_summary is not None:
            chat = (
                await session.execute(
                    select(Chat).where(Chat.id == chat_id)
                )
            ).scalar_one_or_none()
            if chat is not None and not chat.context_summary:
                chat.context_summary = context_summary
        session.add(
            Message(
                chat_id=chat_id,
                role="assistant",
                content=content,
                is_error=is_error,
                meta=meta or {},
            )
        )
        await session.commit()


async def _agent_config(chat_id: str) -> dict:
    """Return the LangGraph thread config for a chat."""
    return {"configurable": {"thread_id": chat_id}}


async def _final_assistant_text(agent, config: dict) -> str:
    """Return the last assistant message committed to the thread, if any.

    ``stream_mode="messages"`` also surfaces token fragments from internal
    model calls (e.g. the build/validate repair passes), so the persisted
    reply must come from the committed state, not the joined stream buffer.
    """
    state = await agent.aget_state(config)
    if state is None:
        return ""
    for message in reversed(state.values.get("messages", [])):
        if isinstance(message, AIMessage):
            return _content_text(message)
    return ""


async def _run_meta(agent, config: dict) -> tuple[dict, bool]:
    """Derive the Message.meta payload and is_error flag from post-run state.

    Pending approval → phase "confirm" with an approval marker (the frontend
    reads this to show the Approve/Reject UI). Otherwise report the terminal
    phase, the validated workflow JSON when it exists, and the validation
    outcome (including errors) so the client can render failure clearly.
    """
    state = await agent.aget_state(config)
    if state is None:
        return {}, False
    values = state.values
    phase = values.get("phase")

    if await _has_pending_approval(agent, config):
        return {"phase": "confirm", "approval": {"status": APPROVAL_PENDING}, "spec": values.get("spec")}, False

    meta: dict = {"phase": phase} if phase else {}
    validation_status = values.get("validation_status")
    if validation_status in ("failed_after_retries", "build_failed"):
        meta["validation"] = {
            "status": validation_status,
            "errors": values.get("validation_errors", []),
        }
        if values.get("workflow_json") is not None:
            meta["workflow_json"] = values["workflow_json"]
        return meta, True

    if phase == "done" and validation_status == "valid":
        meta["validation"] = {"status": validation_status, "errors": []}
        if values.get("workflow_json") is not None:
            meta["workflow_json"] = values["workflow_json"]
        if values.get("workflow_name"):
            meta["workflow_name"] = values["workflow_name"]
    return meta, False


async def _assistant_stream(
    request: Request,
    chat_id: str,
    content: str,
    lock: asyncio.Lock,
) -> AsyncIterator[str]:
    """Run the agent on a new user message and stream the reply.

    Consumes the LangGraph stream in ["messages", "custom"] mode: message
    chunks become Text-Stream-Protocol chunks, custom status payloads are
    forwarded to keep the connection alive during long build/validate passes.
    Only assistant message text is buffered into the persisted row; status
    lines are dropped from storage. Exactly one assistant Message row is
    flushed once the stream finishes (or is aborted). The per-chat lock is
    held for the whole run and released here, in the generator, because the
    StreamingResponse iterates this generator after the handler returns.
    """
    agent = request.app.state.agent
    config = await _agent_config(chat_id)
    buffer: list[str] = []
    is_error = False
    request_task = asyncio.current_task()
    try:
        async for event in agent.astream(
            {"messages": [HumanMessage(content=content)]},
            config=config,
            stream_mode=["messages", "custom"],
        ):
            mode, payload = event
            if mode == "custom":
                status_text = _status_text(payload)
                if status_text:
                    yield status_text
                continue
            if isinstance(payload, tuple) and isinstance(payload[0], AIMessage):
                text = _content_text(payload[0])
                if text:
                    buffer.append(text)
                    yield text
    except BaseException:
        is_error = True
        raise
    finally:
        try:
            reply = await _final_assistant_text(agent, config) or "".join(buffer)
            meta, meta_is_error = await _run_meta(agent, config)
            is_error = is_error or meta_is_error
            context_summary = None
            if meta.get("phase") == "confirm":
                context_summary = reply or None
            if request_task is not None and request_task.cancelling():
                _fire_and_forget(
                    _flush_assistant_row(
                        chat_id, reply, is_error=True, meta=meta, context_summary=context_summary
                    )
                )
            else:
                await _flush_assistant_row(
                    chat_id, reply, is_error, meta=meta, context_summary=context_summary
                )
        finally:
            lock.release()


async def _approve_stream(
    request: Request,
    chat_id: str,
    approved: bool,
    feedback: str | None,
    lock: asyncio.Lock,
) -> AsyncIterator[str]:
    """Resume an interrupted run with the approval decision and stream the result."""
    agent = request.app.state.agent
    config = await _agent_config(chat_id)
    buffer: list[str] = []
    is_error = False
    request_task = asyncio.current_task()
    try:
        async for event in agent.astream(
            Command(resume={"approved": approved, "feedback": feedback}),
            config=config,
            stream_mode=["messages", "custom"],
        ):
            mode, payload = event
            if mode == "custom":
                status_text = _status_text(payload)
                if status_text:
                    yield status_text
                continue
            if isinstance(payload, tuple) and isinstance(payload[0], AIMessage):
                text = _content_text(payload[0])
                if text:
                    buffer.append(text)
                    yield text
    except BaseException:
        is_error = True
        raise
    finally:
        try:
            reply = await _final_assistant_text(agent, config) or "".join(buffer)
            meta, meta_is_error = await _run_meta(agent, config)
            is_error = is_error or meta_is_error
            if request_task is not None and request_task.cancelling():
                _fire_and_forget(
                    _flush_assistant_row(chat_id, reply, is_error=True, meta=meta)
                )
            else:
                await _flush_assistant_row(chat_id, reply, is_error, meta=meta)
        finally:
            lock.release()


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
    request: Request,
    payload: MessageCreate,
    chat: Chat = Depends(get_owned_chat),
    session: AsyncSession = Depends(get_session),
):
    lock = _chat_lock(chat.id)
    await lock.acquire()
    agent = request.app.state.agent
    config = await _agent_config(chat.id)
    try:
        if await _has_pending_approval(agent, config):
            lock.release()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This chat is waiting for your approval — approve or reject the "
                "pending build before sending a new message.",
            )

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
            Message(
                chat_id=chat.id,
                role="user",
                content=payload.content,
                parent_id=payload.parent_id,
            )
        )
        await session.commit()
    except BaseException:
        lock.release()
        raise

    return StreamingResponse(
        _assistant_stream(request, chat.id, payload.content, lock),
        media_type="text/plain; charset=utf-8",
    )


@router.post("/{chat_id}/approve")
async def approve(
    request: Request,
    payload: ApproveRequest,
    chat: Chat = Depends(get_owned_chat),
    session: AsyncSession = Depends(get_session),
):
    """Apply the approval/rejection decision and resume the interrupted run.

    Persists the decision on the pending confirmation message, then resumes
    the LangGraph thread with Command(resume=...) and streams the post-decision
    continuation (build + validation on approval; replan on rejection).
    """
    lock = _chat_lock(chat.id)
    await lock.acquire()
    try:
        recent = (
            await session.execute(
                select(Message)
                .where(Message.chat_id == chat.id, Message.role == "assistant")
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(10)
            )
        ).scalars().all()

        pending = next(
            (
                message
                for message in recent
                if message.meta.get("approval", {}).get("status") == APPROVAL_PENDING
            ),
            None,
        )
        if pending is None:
            lock.release()
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
    except BaseException:
        lock.release()
        raise

    return StreamingResponse(
        _approve_stream(request, chat.id, payload.approved, payload.feedback, lock),
        media_type="text/plain; charset=utf-8",
    )