import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session, get_session_factory
from app.deps import get_owned_chat, paginate
from app.models import Attachment, Chat, Message, new_id
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


def _chunk_usage(chunk: AIMessage) -> tuple[int | None, int | None]:
    """Extract (input_tokens, output_tokens) from a streamed message chunk.

    The provider populates ``usage_metadata`` only on the final chunk of each
    generation, so a run's totals are the sum over every usage-bearing chunk
    (plan structured call, build tool-loop calls, repair passes all stream).
    """
    usage = getattr(chunk, "usage_metadata", None)
    if not isinstance(usage, Mapping):
        return None, None
    return usage.get("input_tokens"), usage.get("output_tokens")


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
    user_message_id: str | None = None,
    parent_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """Persist one assistant Message row (open its own session; commit).

    The request-scoped session from Depends(get_session) may be torn down when
    the handler's response returns, so this generator opens a fresh session for
    the write. When meta signals a pending approval the chat row's
    context_summary is set to the confirm summary if it isn't already present.

    The user row that triggered the run (when user_message_id is given) is
    backfilled with the run's token totals, the assistant row references its
    parent, and a finalized workflow (phase "done") is stored as an Attachment
    row pointing at a download route that serves it from Message.meta.
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
        if user_message_id is not None:
            await session.execute(
                update(Message)
                .where(Message.id == user_message_id, Message.chat_id == chat_id)
                .values(input_tokens=input_tokens, output_tokens=0)
            )
        assistant = Message(
            chat_id=chat_id,
            role="assistant",
            content=content,
            is_error=is_error,
            meta=meta or {},
            parent_id=parent_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        session.add(assistant)
        await session.flush()

        workflow = (meta or {}).get("workflow_json")
        if (meta or {}).get("phase") == "done" and workflow is not None:
            attachment_id = new_id()
            session.add(
                Attachment(
                    id=attachment_id,
                    message_id=assistant.id,
                    file_name=(meta or {}).get("workflow_name") or "workflow.json",
                    file_type="application/json",
                    file_url=f"/api/v1/chats/{chat_id}/messages/{assistant.id}/attachments/{attachment_id}/download",
                    size_bytes=len(json.dumps(workflow).encode("utf-8")),
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
    user_message_id: str | None = None,
) -> AsyncIterator[str]:
    """Run the agent on a new user message and stream the reply.

    Consumes the LangGraph stream in ["messages", "custom"] mode: message
    chunks become Text-Stream-Protocol chunks, custom status payloads are
    forwarded to keep the connection alive during long build/validate passes.
    Only assistant message text is buffered into the persisted row; status
    lines are dropped from storage. Token usage is summed from the usage-
    bearing chunks (every generation streams once, with totals on its final
    chunk) and written to both the triggering user row and the assistant row.
    Exactly one assistant Message row is flushed once the stream finishes (or
    is aborted). The per-chat lock is held for the whole run and released
    here, in the generator, because the StreamingResponse iterates this
    generator after the handler returns.
    """
    agent = request.app.state.agent
    config = await _agent_config(chat_id)
    buffer: list[str] = []
    is_error = False
    request_task = asyncio.current_task()
    in_tokens = 0
    out_tokens = 0
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
                chunk_in, chunk_out = _chunk_usage(payload[0])
                if chunk_in is not None:
                    in_tokens += chunk_in
                if chunk_out is not None:
                    out_tokens += chunk_out
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
            kwargs = {
                "chat_id": chat_id,
                "content": reply,
                "is_error": is_error,
                "meta": meta,
                "context_summary": context_summary,
                "user_message_id": user_message_id,
                "parent_id": user_message_id,
                "input_tokens": in_tokens or None,
                "output_tokens": out_tokens or None,
            }
            if request_task is not None and request_task.cancelling():
                kwargs["is_error"] = True
                _fire_and_forget(_flush_assistant_row(**kwargs))
            else:
                await _flush_assistant_row(**kwargs)
        finally:
            lock.release()


async def _approve_stream(
    request: Request,
    chat_id: str,
    approved: bool,
    feedback: str | None,
    lock: asyncio.Lock,
    parent_id: str | None = None,
) -> AsyncIterator[str]:
    """Resume an interrupted run with the approval decision and stream the result."""
    agent = request.app.state.agent
    config = await _agent_config(chat_id)
    buffer: list[str] = []
    is_error = False
    request_task = asyncio.current_task()
    in_tokens = 0
    out_tokens = 0
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
                chunk_in, chunk_out = _chunk_usage(payload[0])
                if chunk_in is not None:
                    in_tokens += chunk_in
                if chunk_out is not None:
                    out_tokens += chunk_out
    except BaseException:
        is_error = True
        raise
    finally:
        try:
            reply = await _final_assistant_text(agent, config) or "".join(buffer)
            meta, meta_is_error = await _run_meta(agent, config)
            is_error = is_error or meta_is_error
            kwargs = {
                "chat_id": chat_id,
                "content": reply,
                "is_error": is_error,
                "meta": meta,
                "parent_id": parent_id,
                "input_tokens": in_tokens or None,
                "output_tokens": out_tokens or None,
            }
            if request_task is not None and request_task.cancelling():
                kwargs["is_error"] = True
                _fire_and_forget(_flush_assistant_row(**kwargs))
            else:
                await _flush_assistant_row(**kwargs)
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
            .options(selectinload(Message.attachments))
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

        user_message = Message(
            chat_id=chat.id,
            role="user",
            content=payload.content,
            parent_id=payload.parent_id,
        )
        session.add(user_message)
        await session.commit()
        await session.refresh(user_message)
    except BaseException:
        lock.release()
        raise

    return StreamingResponse(
        _assistant_stream(
            request, chat.id, payload.content, lock, user_message_id=user_message.id
        ),
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
        _approve_stream(
            request, chat.id, payload.approved, payload.feedback, lock, parent_id=pending.id
        ),
        media_type="text/plain; charset=utf-8",
    )


@router.get("/{chat_id}/messages/{message_id}/attachments/{attachment_id}/download")
async def download_attachment(
    message_id: str,
    attachment_id: str,
    chat: Chat = Depends(get_owned_chat),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Serve a stored workflow attachment from the owning message's meta.

    The final validated workflow JSON lives in Message.meta (DB-persisted, so
    it survives the ephemeral Railway disk); this endpoint streams it back as
    an application/json download so the file_url on the Attachment row is
    actionable without any object storage.
    """
    message = (
        await session.execute(
            select(Message).where(Message.id == message_id, Message.chat_id == chat.id)
        )
    ).scalar_one_or_none()
    if message is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found",
        )
    attachment = (
        await session.execute(
            select(Attachment).where(
                Attachment.id == attachment_id, Attachment.message_id == message_id
            )
        )
    ).scalar_one_or_none()
    if attachment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attachment not found",
        )
    workflow = message.meta.get("workflow_json")
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workflow payload not available",
        )
    return Response(
        content=json.dumps(workflow, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{attachment.file_name}"',
        },
    )