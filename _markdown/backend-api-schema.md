# BotChain AI — Backend API & Schema Reference (for the Next.js frontend)

**Source of truth: the FastAPI app in `src/app/` (repo `botchain-ai`).** This doc exists so
frontend agents can build the API calls without reading Python. It describes the **live
contract as of Phase 5** (LangGraph flow + per-message tokens + parent chaining +
workflow attachments). If a field's behavior surprises you, the Python in
`src/app/api/v1/{chats,messages}.py` and `src/app/schemas/{chat,message,common}.py` wins.

---

## 1. Base URL & health

- All business routes live under **`<BACKEND_BASE_URL>/api/v1`**. The base URL is
  env-dependent (local dev `http://127.0.0.1:8000`, later a Railway domain once
  deployed). Confirm the URL you have is right with the **unauthenticated** health probe:
  - `GET /health` → `{"status":"ok"}` (200). Anything else means wrong host/port.
- Recommended wiring: the Next.js app proxies through its own route handler (like the
  current `src/app/api/chat/route.ts`) and the browser never calls the backend directly —
  keeps CORS a non-issue and lets you attach the Kinde token server-side. Direct
  browser calls will work too when `CORS_ORIGINS` is configured.

## 2. Authentication (all routes except `/health`)

- **Header:** `Authorization: Bearer <Kinde access token JWT>` — required on every
  endpoint below; missing/malformed → `401`.
- Backend verifies the JWT against **Kinde's JWKS** (issuer from env, keys cached 10
  min): RS256 signature, `iss`, `exp`, and `aud` when a `KINDE_AUDIENCE` is configured.
  This is a pure resource-server check — no shared secret, no session cookie.
- The token's `sub` claim is the **Kinde user id** (`kp_…`). The backend looks that up
  against the local `users.kinde_id` column to find the local **`User.id` UUID** — the
  id every app row and URL uses. **Use `User.id` in all payloads/URLs, never `kindeId`.**
- `401` bodies (distinguish at the UI):
  - `"Not authenticated"` — header absent/malformed → show login.
  - `"Invalid or expired token"` — signature/issuer/exp/aud failed → refresh token.
  - `"Registration not complete"` — valid JWT but **no `users` row** for that `kinde_id`.
    User creation only happens in the Next.js `/api/auth/sync` route (signup grant
    included); the backend never creates users. If the user is logged in and this 401
    appears, the sync didn't run — surface it as a retry-able error, not a crash.
- All downstream routes are **user-scoped**: a chat/message belonging to another user is
  indistinguishable from a missing one (`404`), never `403`.

## 3. Conventions

| Thing | Rule |
|---|---|
| IDs | Random **UUIDv4 strings** on every row, generated server-side (DB has no auto-increment for them). |
| Timestamps | ISO-8601 with UTC offset (`2026-09-25T12:15:27.096001Z`), server-generated (`created_at`/`updated_at`). |
| Bodies | `Content-Type: application/json` for requests; responses are always JSON except the two streaming endpoints and the attachment download. |
| Pagination | Query `?page=1&page_size=20` (page ≥ 1; page_size 1–100). Response is always `{items, page, page_size, total}`. |
| Errors | Unless noted, error bodies are `{"detail": "human-readable message"}`. `422` = schema validation; `400/401/404/409/503` as documented per route. |
| Soft delete | Deleting a chat sets `deleted_at`; it disappears from all reads but stays in the DB. |
| Token usage | Every message row (user and assistant) carries `input_tokens`/`output_tokens` captured from the real LLM stream. `output_tokens` is `0` on user rows (they produced no completion). |

---

## 4. Endpoint reference

### 4.1 Chats — `POST /api/v1/chats`

Create a chat for the current user.

**Request body** (`application/json`):
```json
{ "title": "My automation", "model": "nemotron-3-ultra:cloud" }
```
| Field | Type | Req | Notes |
|---|---|---|---|
| `title` | `string \| null` | no | max 255; `null`/omitted → server default `"New Chat"` |
| `model` | `string \| null` | no | max 100; `null`/omitted → server default `"claude-sonnet-4-6"` (model actually used is fixed by the backend's `services/llm.py` for now) |

**Response `201` — `ChatOut`:**
```json
{ "id": "…uuid…", "title": "My automation", "model": "claude-sonnet-4-6",
  "pinned": false, "archived": false, "meta": {},
  "created_at": "…", "updated_at": "…" }
```
Full `ChatOut` field list: `id` (str), `title` (str), `model` (str), `pinned` (bool),
`archived` (bool), `meta` (object, currently `{}`), `created_at`/`updated_at` (ISO str).

### 4.2 List chats — `GET /api/v1/chats`

Return the current user's chats, **newest first**, excluding soft-deleted.

Query: `page` (default 1), `page_size` (default 20, max 100).

**Response `200`:**
```json
{ "items": [ /* ChatOut… */ ], "page": 1, "page_size": 20, "total": 3 }
```

### 4.3 Get chat — `GET /api/v1/chats/{chat_id}`

**Response `200` — `ChatOut`.** `404` if missing / soft-deleted / not owned.

### 4.4 Rename chat — `PATCH /api/v1/chats/{chat_id}`

**Request body:** `{ "title": "New name" }` — `title` is **required** (1–255 chars).

**Response `200` — `ChatOut`** with the new title. `404` on bad chat id.

### 4.5 Soft-delete chat — `DELETE /api/v1/chats/{chat_id}`

**Response `204 No Content`** (no body). `404` on bad chat id. Sets `deleted_at`; the
chat is excluded from every list/get from then on.

### 4.6 List messages — `GET /api/v1/chats/{chat_id}/messages`

Full history for resume. **Oldest first** (`created_at asc, id asc`) — unlike chats.

Query: `page`, `page_size`. Each item eagerly includes its `attachments` (when the
message is a finalized workflow, exactly one attachment is expected).

**Response `200`:**
```json
{ "items": [ /* MessageOut… */ ], "page": 1, "page_size": 100, "total": 7 }
```
`MessageOut`:
| Field | Type | Notes |
|---|---|---|
| `id` | string | |
| `chat_id` | string | |
| `parent_id` | string \| null | `null` on user rows (roots); assistant replies carry the id of the user message that triggered them; approve-continuations carry the pending confirm message's id |
| `role` | string | `"user"` \| `"assistant"` (only these two are ever written today) |
| `content` | string | full message text, not accumulated deltas |
| `is_error` | bool | `true` when the run failed or finished with validation errors |
| `input_tokens` | int \| null | this message's run input token count (see §3) |
| `output_tokens` | int \| null | this message's run output token count; `0` on user rows |
| `meta` | object | **phase/state machine — see §6, this is what the UI renders from** |
| `created_at` | ISO string | |
| `attachments` | array | `AttachmentOut[]`; grows on the finalized `done` message |

`AttachmentOut`: `id`, `file_name` (str), `file_type` (str, `application/json` for
workflows), `file_url` (str — **callable backend download URL, see §4.10**),
`size_bytes` (int), `created_at` (ISO string).

### 4.7 Send message → stream — `POST /api/v1/chats/{chat_id}/messages`

Push one user message into the chat and get the agent's streaming reply back.
`content` in `MessageCreate` is **required, min 1 char**. `parent_id` is optional:
when omitted the message is the linear "next" turn; when set it must be an existing
message id **in the same chat** (else `400` — permits future branch/regenerate UIs).

**Request body:**
```json
{ "content": "Whenever someone submits my contact form, post their name and message to #general Slack.", "parent_id": null }
```

**Response `200` — an SSE stream conforming to the AI SDK v7 Data Stream
Protocol / UI Message Stream** (`text/event-stream` +
`x-vercel-ai-ui-message-stream: v1`). Not JSON-as-a-whole; see §5 for the exact
chunk sequence. First event is `{"type":"start"}`, then live `data-status`
parts, then the assistant reply as `text-delta` parts.

| Error | When |
|---|---|
| `409 Conflict` | The chat is already parked at a **pending approval** (a `confirm`-phase message waits for you): body `"This chat is waiting for your approval — approve or reject the pending build before sending a new message."` The user must call §4.8 instead. |
| `400` | `parent_id` given but the message doesn't exist, or belongs to a different chat. |
| `404` `404` | bad chat id. |

**Timing/ordering contract (matters for history):** the **user row is committed
synchronously before streaming** — after this call returns, `GET …/messages` already
shows the new user message. The **assistant row is written after the stream completes**
(backend generator flush). So don't refetch history until the stream ends; once it ends
the assistant row (with tokens, meta, and possibly an attachment) is durable.

### 4.8 Approve / reject → stream — `POST /api/v1/chats/{chat_id}/approve`

Resume an interrupted run with the human's decision. **Required before any workflow is
built**: the graph pauses at the confirm gate and will not continue on its own.

**Request body** (`ApproveRequest`):
```json
{ "approved": true, "feedback": null }
```
| Field | Type | Req | Notes |
|---|---|---|---|
| `approved` | boolean | no | defaults `true`; `false` = reject → agent replans with your feedback |
| `feedback` | string \| null | no | max 2000; use on rejection to tell the agent what to change |

**Response `200` — streaming** (same Data Stream Protocol format as §4.7). On
approval you'll see build `data-status` parts (`Looking that node up…` →
`Assembling…` → `Checking…` → `Fixing validation errors…` → `Workflow
validated. Done.`) then the final text `text-delta` parts, and the resulting
`done` message carries the workflow + attachment. On rejection the agent
replans (status `Planning…`) and parks at the confirm gate again.

**Side effect:** the still-pending `confirm` message's `meta.approval` is mutated
in-place to `{"status": "approved"|"rejected", "feedback": …}`.

| Error | When |
|---|---|
| `409 Conflict` | No pending approval exists for this chat (nothing to resume) — body `"No pending approval for this chat"`. |
| `404` | bad chat id. |

### 4.9 Get credits — `GET /api/v1/credits` *(stub, currently 404)*

The router is mounted under `/api/v1/credits` but defines **no routes yet**. Do not call
it in the UI; billing is out of scope this phase (signup grant + wallet exist in the DB;
a balance endpoint arrives with backend billing work).

### 4.10 Download workflow attachment — `GET /api/v1/chats/{chat_id}/messages/{message_id}/attachments/{attachment_id}/download`

Stream the finalized workflow file. The URL is handed to you verbatim on every
`AttachmentOut.file_url` (build it from `attachment.file_url` on the client — don't
reconstruct it). The JSON is served from the message's `meta.workflow_json` (DB-backed),
so it survives anywhere the app runs.

**Response `200`:** `Content-Type: application/json`,
`Content-Disposition: attachment; filename="workflow.json"` (or the `workflow_name`).
Body = the pretty-printed n8n workflow JSON. `404` if the message/attachment is missing
or the message has no stored `workflow_json`.

### 4.11 Webhooks — `/api/v1/webhooks/*` *(stub — no routes yet)*

Mounted, empty. Payment webhooks (Razorpay) land here later; nothing to call now.

---

## 5. Streaming wire format (Phase-6 integration contract)

The two streaming endpoints (§4.7, §4.8) return an **SSE stream** conforming to the
AI SDK v7 **Data Stream Protocol / UI Message Stream**, so `useChat()` consumes it
directly — **no transport config on the client** (v7 default) and **no translation in
the route handler**. Headers:

| Header | Value |
|---|---|
| `content-type` | `text/event-stream; charset=utf-8` |
| `x-vercel-ai-ui-message-stream` | `v1` *(this header is what makes `useChat` parse the body as a UI message stream)* |
| `cache-control` | `no-cache` |
| `connection` | `keep-alive` |
| `x-accel-buffering` | `no` |

Every chunk is one SSE `data:` line, UTF-8 — `data: <json>\n\n`. The sequence is:

1. `{"type":"start"}` — run begins.
2. One or more **live-only status parts** for the graph's phase heartbeats.
   `transient: true` means `useChat` hands the payload to your `onData` callback
   **without** persisting it as a message part:
   ```json
   {"type":"data-status","data":{"status":"Planning…"},"transient":true}
   ```
   Known statuses (all spelled exactly): `Planning…`, `Looking that node up…`,
   `Assembling the workflow file…`, `Checking the workflow…`,
   `Fixing validation errors…`, `Workflow validated. Done.` There is **no
   "confirming" status** — the graph simply ends the stream at the approval gate;
   detect pending approval from the post-stream state (see below).
3. The assistant reply as a text part — the id is a **fresh uuid per text segment**
   (reopening after a status interruption must generate a new id, not reuse the
   previous one, or the SDK merges the second segment into the first part):
   `{"type":"text-start","id":"<uuid>"}` → repeated
   `{"type":"text-delta","id":"<uuid>","delta":"…"}` → `{"type":"text-end","id":"<uuid>"}`.
   Concatenate the `delta` values of one segment for its text. If a status interrupts open
   text (it never does today — the reply always streams last), the backend closes
   `text-end` before the `data-status`, so deltas stay contiguous and the reopened
   segment gets a fresh id.
4. `{"type":"finish","finishReason":"stop"}` then the terminal `data: [DONE]\n\n`.

**Error mid-stream:** an `{"type":"error","errorText":"…"}` chunk then the socket
closes — no `finish`, no `[DONE]`.

After the stream ends, follow up with `GET …/messages`: the committed assistant row
(plus its `meta`/tokens/attachment) is the authoritative record. The agent-internal
model calls (structured plan, build self-repair) stream status/`text` frames too; only
the final committed text is the real message.

Important constraint the route handler must respect:
- **The proxy route needs an explicit `maxDuration` — do not just delete/omit it.**
  The confirm→build→validate pipeline routinely takes minutes (verified live: one
  run streamed ~97k input tokens over build + repair passes; ~135s+ worst case).
  Omitting the export reverts to the plan default, not "unlimited", and every plan
  still enforces a hard ceiling (`504 FUNCTION_INVOCATION_TIMEOUT`) counting total
  request + streamed-response time — distinct from any idle timeout the status
  heartbeats handle. Ceilings as of 2026-09: with Fluid Compute, default 300s on all
  plans, Hobby max 300s / Pro+Enterprise max 800s (1800s beta); on non-Fluid
  deployments Hobby is capped at 60s max, where a 135s build cannot run at any
  setting. Set `maxDuration` to 300s+ and confirm the project's actual ceiling
  (plan + Fluid on/off) ≥ worst-case build time — see the Phase 6 frontend plan.
- A `plan`/`confirm` turn may pause at the **confirm gate**: `finish` + `[DONE]` arrive
  and the last committed assistant row has `meta.phase == "confirm"` with
  `approval.status == "pending"`. The UI must switch to the approve/reject affordance
  (§4.8), not keep sending messages.

---

## 6. Message meta — the agent state machine the UI renders

`Message.meta` signals what phase the conversation is in. It is **the only source of
truth for the phase** (there is no `status` column on `Message`).

| `meta.phase` | Happens when | `meta` shape | UI behavior |
|---|---|---|---|
| `plan` | Every planner turn while the spec is being gathered (may be several messages before confirm) | `{"phase":"plan"}` | Normal chat; show streamed text. |
| `confirm` | The spec is complete and the graph is **waiting at the approval interrupt** | `{"phase":"confirm","approval":{"status":"pending"},"spec":{…}}` | **Stop text input.** Show the spec summary + Approve / Reject (with feedback). Call §4.8. After approval, `meta.approval` mutates to `{"status":"approved"\|"rejected","feedback":…}`. |
| `done` | Build+validate converged after approval | `{"phase":"done","validation":{"status":"valid","errors":[]},"workflow_json":{…},"workflow_name":"…"}` | Success state. `attachments` on this message has the downloadable workflow (use `attachment.file_url` → §4.10). |
| `done` (with error) | Validation never converged (≤3 retries) | `{"phase":"done","validation":{"status":"failed_after_retries"\|"build_failed","errors":[…]},"workflow_json"?:{…}}` and `is_error: true` | Show failure + remaining issues; offer a light retry (send a new message). |

`spec` under `confirm` is the gathered requirements spec:
```json
{ "goal": "…", "trigger_type": "webhook", "services_involved": ["slack"],
  "conditions_logic": "…", "data_flow": "…", "constraints": "…",
  "open_questions": [] }
```

## 7. Shared data model (matches `prisma/contract.prisma`)

Both apps read/write the **same Neon tables**. Backend-relevant subset:

```
users                id UUID, kinde_id UQ, email UQ, first_name?, last_name?, created_at, updated_at
chats                id, user_id FK, title, model, system_prompt?, context_summary?, pinned, archived,
                     meta json, created_at, updated_at, deleted_at?
messages             id, chat_id FK, parent_id FK self?, role, content, input_tokens?, output_tokens?,
                     credits_cost?, is_error, meta json, created_at
attachments          id, message_id FK, file_name, file_type, file_url, size_bytes, created_at
credit_wallets       user_id PK/FK, balance, updated_at
credit_transactions  id, user_id FK, type, amount, balance_after, reference_type?, reference_id?,
                     provider?, provider_ref?, meta, created_at
payment_topups       id, user_id FK, provider, provider_payment_id UQ, amount_fiat, currency, credits_purchased,
                     status, created_at, completed_at?
```

Notes for the frontend:
- `chats.context_summary` is set by the backend to the confirm summary on the first
  confirm turn (used for conversation memory, not displayed).
- `messages.meta` is a JSON column; the SQLAlchemy side reads/writes it as `dict`.
- Token/credits fields: `credits_cost` stays `NULL` until billing exists.

## 8. Reference end-to-end (happy path) for UI state

1. `POST /api/v1/chats` → `ChatOut.id` (this is the route segment `/users/{User.id}/…/{Chat.id}`).
2. `POST /api/v1/chats/{id}/messages` `{content}` → stream: `Planning…` + text. Repeat;
   assistant rows carry `meta.phase: "plan"`.
3. Eventually a stream ends with the last assistant row `meta.phase: "confirm"`,
   `approval.status: "pending"`, `spec` populated. Render approve/reject.
4. `POST /api/v1/chats/{id}/approve` `{approved:true}` → stream build statuses. On
   finish, `GET …/messages` shows the `done` row with `workflow_json` in meta and an
   `attachments` entry whose `file_url` downloads the file (§4.10).
5. Rejection path: `{approved:false, feedback:"use a GET request"}` → streams `Planning…`,
   parks at confirm again.

## 9. What is NOT implemented yet (do not design the UI around it)

- **Thread forking / branching:** lineage (`parent_id`) and the send-route parent
  validation exist, but the agent still runs **one linear LangGraph thread per chat**. A
  `parent_id` at mid-history is stored but the graph continues linearly.
- **Credits / billing balance endpoint** (§4.9) and **webhooks** (§4.11) — empty stubs.
- **Tool-call / reasoning indicators** in the stream (statuses are the stand-in until a
  protocol upgrade in Phase 6).
- **File uploads / objects:** attachments are metadata + a DB-served JSON endpoint only
  (§4.10); there is no object storage yet, so the UI must fetch `file_url` to get bytes.