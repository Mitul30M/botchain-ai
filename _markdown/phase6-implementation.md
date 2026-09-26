# Phase 6 — Streaming: Implementation, Tests & Wire-Format Notes

**Implemented + verified 2026-09-26.** The message and approve routes now return the
AI SDK v7 **Data Stream Protocol / UI Message Stream** (`x-vercel-ai-ui-message-stream: v1`)
instead of a plain-text stream. Status heartbeats ride as transient `data-status`
parts; the assistant reply streams as `text-delta` fragments; `useChat` consumes the
whole thing with zero transport config. 43 pytest green (5 new DSP tests), ruff clean.

---

## 1. Why not the checklist's "Text Stream Protocol first"

The checklist / Q7 settled decision said start with plain incremental text (TSP) and
upgrade to DSP later for tool indicators. Evidence during planning (checked against the
installed SDK source, not memory) flipped that:

- The installed AI SDK v7 **defaults `useChat` to the data/UI-message protocol** and the
  frontend's dummy `src/app/api/chat/route.ts` already emits it — so DSP means **zero
  frontend transport change**, whereas TSP would require adding
  `streamProtocol: "text"` + `TextStreamChatTransport` and changing the dummy route.
- The agent's **status heartbeats** (`Planning…`, `Assembling the workflow file…`,
  `Fixing validation errors…`) can't ride a plain-text stream. The long build/validate
  passes (multi-minute, ~100k-token generations live-verified in Phase 5) need the
  live status live updates; TSP would force filtering them out entirely.
- The old render separately: TSP → statuses were woven into the text with no framing, so
  a proxy couldn't cleanly strip them.
- Q7's caution was the official `vercel/ai` FastAPI DSP example being broken — we don't
  use it; we hand-roll a ~60-line SSE encoder against the SDK's own installed chunk
  schema (`UI_MESSAGE_STREAM_HEADERS` + `process-ui-message-stream`).

Full decision + wire spec: `_markdown/phase6/backend-phase6-plan.md`.

## 2. What was implemented

### 2.1 `src/app/streaming.py` (new, ~70 lines)

| Symbol | Detail |
|---|---|
| `UI_STREAM_HEADERS` | `content-type: text/event-stream`, `cache-control: no-cache`, `connection: keep-alive`, `x-vercel-ai-ui-message-stream: v1`, `x-accel-buffering: no` |
| `_sse(payload)` | one `data: <json>\n\n` line; `json.dumps(..., ensure_ascii=False)` |
| `_DONE_EVENT` | `data: [DONE]\n\n` terminator |
| `_ui_stream(events)` | async generator: `("status", s)` → `data-status` part (`transient: true`, `data:{"status": s}`); `("text", t)` → `text-start`/`text-delta`…/`text-end` (`id:"0"`). `text-end` closes the text part before any interrupting status so deltas stay contiguous. Opens with `start`, closes with `finish{finishReason:"stop"}` + `[DONE]`. Exceptions from the inner iterator emit `{"type":"error","errorText":"…"}` then **re-raise**, so the caller's `finally` flush still runs and the row persists `is_error=True` |

### 2.2 `src/app/api/v1/messages.py` (minimal diff)

- `_assistant_stream` / `_approve_stream`: changed the yield sites only —
  `yield status_text` → `yield ("status", status_text)` and `yield text` →
  `yield ("text", text)`. Buffering, token summing, `_run_meta`,
  `_flush_assistant_row`, lock release in `finally`, and the 409/400/404
  pre-stream errors are untouched (behavioral contract preserved).
- Route handlers wrap the streams: `StreamingResponse(_ui_stream(...),
  media_type="text/event-stream", headers=UI_STREAM_HEADERS)`.

## 3. Wire format (what the frontend receives)

```
content-type: text/event-stream
x-vercel-ai-ui-message-stream: v1
…
data: {"type":"start"}
data: {"type":"data-status","data":{"status":"Planning…"},"transient":true}
data: {"type":"text-start","id":"0"}
data: {"type":"text-delta","id":"0","delta":"…"}   # one per streamed fragment
data: {"type":"text-end","id":"0"}
data: {"type":"finish","finishReason":"stop"}
data: [DONE]
```

`data-status` parts are `transient`, so `useChat` passes them to the app's `onData`
callback (status banner) and does **not** persist them as message parts. The terminal
buffer for a `confirm`-parked run ends at `finish` + `[DONE]` with no text part — the
frontend detects pending approval from the committed row's `meta.phase == "confirm"`
(`approval.status == "pending"`), not from the stream. In-stream errors emit
`{"type":"error","errorText":…}` (no `finish`/`[DONE]`); pre-stream HTTP errors
(409/400/404) remain normal JSON.

## 4. Tests — `tests/test_streaming_dsp.py` (5 new)

1. **Headers** — exact media type + guard headers, matching the SDK's own
   `UI_MESSAGE_STREAM_HEADERS`.
2. **Ordering/validity** — every line parses as `data: <json>`; exact sequence
   start → data-status → text-start → text-delta → text-delta → text-end →
   data-status → finish → `[DONE]`; terminator exactly once; deltas concatenate to
   the full reply; status text never leaks into `text-delta`.
3. **Statuses stay out of text** — a status-only stream yields no `text-*` parts.
4. **Error path** — `error` chunk emitted (no `finish`, no `[DONE]`), exception
   re-raised.
5. **Full run via `_assistant_stream`** — fake agent + fake SQLAlchemy session: the
   decoded reply equals the persisted assistant row content + `meta`; per-chat lock
   released; commit happened (happy path, plus the existing error-path route tests
   assert the `is_error` row).

The two pre-existing route-streaming tests were updated to the new media type and
chunk-parsed bodies rather than asserting `text/plain`. `uv run pytest` → 43 passed;
`uv run ruff check src tests` clean.

## 5. Docs updated

- `_markdown/python-fastapi-backendchecklist.md` — Phase 6 items ticked (or blow
  buffering), DSP decision annotated.
- `_markdown/backend-setup-qa.md` (Q7 streaming) — reworded to DSP end-to-end; TSP
  shelved for no-status endpoints; FastAPI-example caution resolved by the hand-rolled
  encoder.
- `_markdown/backend-api-schema.md` (+ mirrored copy in `_nextjs_repo_context/`) —
  §5 rewritten from the raw-text contract to the exact DSP wire spec (headers,
  chunk sequence, status list, error shapes); §4.7/§4.8 response rows updated.
- `AGENTS.md` — status paragraph + milestone row 6 marked delivered; settled-decision
  "Streaming" bullet amended to DSP (2026-09-26).

## 6. Follow-ups (next phases)

- **Live curl smoke + proxy e2e** — deferred to the frontend-phase wiring run (needs
  seeded user, live Neon compute, Ollama + n8n-mcp up; owner runs it).
- **TSP uses in later phases** — only if a `/completion`-style endpoint with no status
  data ever appears; today every streamed endpoint carries statuses, so DSP everywhere.
- **Tool-call/reasoning parts** — DSP's richer parts (`tool-call`, `reasoning`) are the
  natural home when the UI wants to surface which nodes the build phase digs up; the
  encoder only needs to forward more event kinds.