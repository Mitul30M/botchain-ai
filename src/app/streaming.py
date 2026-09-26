import json
from collections.abc import AsyncIterator

UI_STREAM_HEADERS = {
    "content-type": "text/event-stream",
    "cache-control": "no-cache",
    "connection": "keep-alive",
    "x-vercel-ai-ui-message-stream": "v1",
    "x-accel-buffering": "no",
}

_DONE_EVENT = "data: [DONE]\n\n"

_ERROR_TEXT = "The build hit an unexpected error — see history."


def _sse(payload: dict) -> str:
    """Encode one UI-message-stream chunk as an SSE data line."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _ui_stream(events: AsyncIterator[tuple[str, str]]) -> AsyncIterator[bytes]:
    """Encode tagged ("status"|"text", str) events into the AI SDK data stream.

    ``("status", ...)`` becomes a live-only ``data-status`` part (transient, so
    useChat consumes it via onData without persisting it into message parts);
    ``("text", ...)`` becomes ``text-delta`` fragments of the single assistant
    reply. The text part is closed (``text-end``) before any status interrupts
    it, so ``text-delta`` fragments stay contiguous per provider convention.
    The stream opens with ``start`` and closes with ``finish`` + [DONE].
    Exceptions from the inner iterator emit an ``error`` chunk then re-raise,
    so the producer's own finally/flush logic still runs and the connection is
    closed as an error.
    """
    yield _sse({"type": "start"}).encode("utf-8")
    text_open = False
    try:
        async for kind, value in events:
            if kind == "status":
                if text_open:
                    yield _sse({"type": "text-end", "id": "0"}).encode("utf-8")
                    text_open = False
                yield _sse(
                    {"type": "data-status", "data": {"status": value}, "transient": True}
                ).encode("utf-8")
            elif kind == "text":
                if not text_open:
                    yield _sse({"type": "text-start", "id": "0"}).encode("utf-8")
                    text_open = True
                yield _sse({"type": "text-delta", "id": "0", "delta": value}).encode(
                    "utf-8"
                )
    except BaseException:
        yield _sse({"type": "error", "errorText": _ERROR_TEXT}).encode("utf-8")
        raise
    if text_open:
        yield _sse({"type": "text-end", "id": "0"}).encode("utf-8")
    yield _sse({"type": "finish", "finishReason": "stop"}).encode("utf-8")
    yield _DONE_EVENT.encode("utf-8")