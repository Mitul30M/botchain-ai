# Phase 6 — Streaming (backend): FastAPI → AI SDK Data Stream Protocol

**Decision recorded (2026-09-26):** Phase 6 standardizes on the **Data Stream
Protocol / UI Message Stream** (`x-vercel-ai-ui-message-stream: v1`), NOT the
previously-written "Text Stream Protocol first" in the checklist / AGENTS.md /
`backend-setup-qa.md` Q7. Reason: the installed AI SDK v7 defaults `useChat` to
this protocol (zero frontend transport config — the frontend's dummy route
already speaks it), and the agent's status heartbeats (`Planning…`,
`Assembling the workflow file…`, `Fixing validation errors…`) cannot ride in a
plain-text stream. Text Stream Protocol stays only as a future option for
`/completion`-style endpoints that don't need status data. This plan updates
the three source-of-truth docs accordingly.

## 1. Goal

> ✅ **Implemented + verified 2026-09-26** (44 pytest green, ruff clean;
> implementation record in `_markdown/phase6-implementation.md`).

Both streaming endpoints — `POST /api/v1/chats/{chat_id}/messages` and
`POST /api/v1/chats/{chat_id}/approve` — return an SSE stream conforming to the
AI SDK v7 **UI Message Stream** wire format, so the Next.js proxy can pipe it
straight through to `useChat` without any protocol translation.

## 2. Wire format (standard, from the installed SDK, not memory)

### Response headers (exactly)
```
content-type: text/event-stream
cache-control: no-cache
connection: keep-alive
x-vercel-ai-ui-message-stream: v1
x-accel-buffering: no
```
(Matches `UI_MESSAGE_STREAM_HEADERS` in `ai/src/ui-message-stream/ui-message-stream-headers.ts`.)

### SSE framing
One event per chunk: `data: <json>\n\n` (blank line terminates each event).
Terminator: `data: [DONE]\n\n`. `json.dumps(..., ensure_ascii=False)` — status
and reply text are user-visible UTF-8.

### Chunk sequence (minimal set for this backend)
```
data: {"type":"start"}                              # messageId optional → SDK auto-ids the assistant message
data: {"type":"data-status","data":{"status":"Planning…"},"transient":true}
data: {"type":"text-start","id":"<uuid>"}
data: {"type":"text-delta","id":"<uuid>","delta":"..."}  # one per streamed text fragment
data: {"type":"text-end","id":"<uuid>"}
data: {"type":"finish","finishReason":"stop"}
data: [DONE]
```
- `data-*` chunk shapes verified against `ui-message-chunks.ts` (`z.custom<`data-${string}>``, fields `id?`, `data`, `transient?`).
- `transient: true` (schema field `transient`): status parts are consumed live via
  `useChat`'s `onData` and are **not** persisted into the client `message.parts`
  (`process-ui-message-stream.ts`: "transient parts are not added to the message
  state"). Exactly what progress heartbeats want.
- No `finish-step` needed for the linear one-assistant-message-per-run flow; a
  single `finish` ends the run. `finishReason` `stop` unless the run errored,
  then omit it / use `error` (see §4 error hardening).
- Statuses NEVER become `text-delta`. Only actual assistant reply text does.
- The same shape is emitted by both endpoints (approve = continuation run).

## 3. Backend code changes — `src/app/api/v1/messages.py` (plus one helper module)

> ✅ **Done 2026-09-26.** Changes shipped exactly as planned below, then verified —
> see §4 for the green gate and `_markdown/phase6-implementation.md`.

### New helper module `src/app/streaming.py`
- `UI_STREAM_HEADERS: dict[str, str]` (the 5 headers above, public).
- `_sse(payload: dict) -> str` → `data: <json>\n\n`; `_DONE_EVENT = "data: [DONE]\n\n"`.
- `_ui_stream(events: AsyncIterator[tuple[str, str]]) -> AsyncIterator[bytes]`:
  consumes tagged events `("status", text)` | `("text", text)` and encodes the
  DSP sequence: `{"type":"start"}` first, `data-status` (with `transient:true`
  and `data:{"status":...}`) for statuses, `text-start`/`text-delta`…/`text-end`
  (a **fresh uuid part id per text segment** — reusing one id across segments
  would make the SDK merge later prose into the first block, see vercel/ai PR
  #15254-area work) around the text pieces, terminating `{"type":"finish",
  "finishReason":"stop"}` + `[DONE]`. The text part is closed with `text-end`
  before any status interrupts it (keeps `text-delta` fragments contiguous per
  provider convention). Must not swallow exceptions — the flusher in the calling
  generator's `finally` still runs when the wrapper re-raises.

### Rework the two generators (minimal diff — 4 yield sites each)
- `_assistant_stream`: `yield status_text` → `yield ("status", status_text)`;
  `yield text` → `yield ("text", text)`. Buffering, token summing, `_run_meta`,
  `_flush_assistant_row`, and lock release in `finally` stay **exactly as-is**
  (behavioral contract with Phase 7/DB is untouched).
- `_approve_stream`: same two yield changes.

### Route handlers
- `send_message`: `StreamingResponse(_ui_stream(_assistant_stream(...)),
  media_type="text/event-stream", headers=_UI_STREAM_HEADERS)`.
- `approve`: same wrap for `_approve_stream`.
- Pre-stream HTTP errors (409 pending-approval, 400 parent, 404 chat) stay as
  normal JSON error responses — they arrive before any stream starts and the
  proxy maps them for `useChat`'s `error`.

### Error hardening (small, in `_ui_stream`)
- Wrap the inner iterator in try/except; on exception emit
  `{"type":"error","errorText":"The build hit an unexpected error — see history."}`
  then re-raise so the generator's flush logic still runs and the row persists
  with `is_error=True`. Keep it a generic message — don't leak internals. (Only
  after error text is defined in `useChat` — confirmed `{"type":"error",
  "errorText"}` exists in the chunk schema.)

### Keep-alive / buffering notes
- SSE is self-keep-alive; the long build/validate gaps between statuses are fine.
- uvicorn/http1.1 flushes per yield; `X-Accel-Buffering: no` covers nginx/Railway
  proxies. Confirm no gzip/compression middleware is added later (compression
  conflicts with streaming).

## 4. Verification gate

> ✅ **Green 2026-09-26.** Items 1–2 done in this phase:
> `uv run pytest` → **44 passed** (38 existing, of which the two old route tests
> were updated to assert the new `text/event-stream` + `x-vercel-ai-ui-message-stream: v1`
> headers and chunk-parsed bodies, plus 6 new DSP tests), `uv run ruff check src tests`
> clean. Full description of the planned gate (kept for reference):

1. `uv run pytest` — existing 38 stay green (the two old route tests that
   asserted `text/plain` streaming get updated to the new media type + chunk
   assertions).
2. New tests in `tests/test_streaming_dsp.py`:
   - Response headers: media type + the guard headers.
   - Ordering/validity: every line parses as `data: <json>`; sequence
     start → data-status* → text-start → text-delta* → text-end → finish →
     [DONE]; terminator exactly once.
   - Statuses land in `data-status` with `transient: true` and never in
     `text-delta`.
   - A fake-agent full run decodes to the same reply text that gets persisted
     (text-delta deltas concatenate === row content).
   - Errors: `{"type":"error","errorText":…}` chunk precedes re-raise; no
     `finish`/`[DONE]` after an error.
3. **Live curl smoke** (real agent: Ollama + n8n-mcp running, same as Phase 5):
   `curl -N -H "Authorization: Bearer …" -H "Content-Type: application/json"
   -d '{"content":"<a notebooks/testcases.md prompt>"}' 127.0.0.1:8734/api/v1/chats/<id>/messages`
   → observe SSE lines incl. `data-status` heartbeats + `text-delta` + `[DONE]`.
   Also smoke `/approve` continuation. **Exercise at least one multi-status run
   watching the raw lines for the reopen path** (status interrupts open text, then
   text resumes): each `text-start` must carry a **fresh id**, never a reuse —
   this is the vercel/ai id-reuse regression the encoder fix guards. —
   **Deferred to the frontend-phase wiring run** (requires a seeded user, live Neon
   compute, Ollama + n8n-mcp all up; the repo-owner runs first-deploy smokes).
   Item 4 covers the client-side half.
4. End-to-end through the Next.js proxy (coordinated with the frontend plan):
   tokens render incrementally, status banner updates live, message persists,
   confirm→approve path streams the continuation, workflow attachment downloads.
   — **Frontend-phase deliverable** (`_markdown/phase6/nextjs-app-phase6-plan.md`).

## 5. Docs to update (part of this phase)

> ✅ All done 2026-09-26.

- `_markdown/python-fastapi-backendchecklist.md` — tick Phase 6 items; annotate
  the Text-Stream-Protocol-first item with the DSP decision + pointer here. ✅
- `_markdown/backend-setup-qa.md` (Q7 streaming sub-section) — record decision:
  DSP (UI Message Stream) end-to-end; TSP shelved; FastAPI-example caution
  resolved by hand-rolling the small SSE encoder. ✅
- `_nextjs_repo_context/backend-api-schema.md` — §5 "Streaming" rewrite: exact
  wire format above, status `data-status` contract, error shapes (pre-stream 4xx
  JSON vs in-stream `error` chunk); update the two endpoint rows. ✅ (Note: file
  now lives at `_markdown/backend-api-schema.md`.)
- `AGENTS.md` — "Streaming" settled-decision bullet → Data Stream Protocol first;
  milestone row 6 line => delivered; status paragraph notes Phase 6 done. ✅
- `_markdown/phase6-implementation.md` — written post-verification, mirroring the
  Phase 5 doc style. ✅

## 6. Out of scope (explicit follow-ups)

- Reasoning / tool-input DSP parts for the Build phase ("what the agent is
  doing") — the natural DSP upgrade later.
- Native streaming of the approve continuation through `useChat` (Phase 6
  frontend plan uses a separate streaming fetch + history refresh).
- Per-run `messageId` reconciliation (`start.messageId` omitted for now; the
  client reconciles via `GET /messages` refresh).
- No Text Stream Protocol route is retained.