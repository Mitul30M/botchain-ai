# Phase 5 — LangGraph Flow: Implementation, Tests & Verified Outputs

**Verified 2026-09-24 (graph + concurrent smoke), 2026-09-25 (modular prompts + real
HTTP/Neon DB-persistence e2e, + token-usage / parent-chaining / attachments e2e).** The
Plan → Confirm → Build → Validate agent is ported into `services/agent.py`, wired into
the FastAPI app, and verified **end-to-end against the real stack** (Ollama Cloud +
n8n-mcp + a validated 2-node n8n workflow) plus a concurrent-build isolation smoke,
plus a full HTTP-route run that persisted every row in Neon, plus a live e2e proving
per-message token usage, `parent_id` chaining and workflow-as-attachment delivery.
38 pytest green, ruff clean.

---

## 1. What was implemented

### 1.1 The graph — `src/app/services/agent.py` (639 lines)

```
START → plan ⇄ (loops with the user until the spec is complete & no open questions)
              ↓
        confirm (LangGraph interrupt: plain-English spec summary — build it?)
              ↓  user approves via POST /api/v1/chats/{id}/approve  (Command(resume=...))
        build → validate ⇄ build (self-repair on validation errors, ≤3)
              ↓  (validation passes)
        done → workflow JSON + validation result persisted to Message.meta
```

| Piece | Detail |
|---|---|
| `AgentState` | `TypedDict(total=False)`: `messages` (add_messages), `phase`, `spec`, `workflow_json`, `workflow_name`, `validation_status`, `validation_errors`, `retry_count`, `build_feedback`, `confirm_summary` |
| `PlanOutput` | Pydantic structured output: `message`, `ready_to_confirm`, `goal`, `trigger_type`, `services_involved`, `conditions_logic`, `data_flow`, `constraints`, `open_questions` |
| `PLAN_SYSTEM_PROMPT` | Interviewer persona: one focused question at a time, infer what's reasonable, curated ~16-node surface (Webhook, Schedule/Form/Manual Trigger, IF, Switch, Set, Code, Merge, Filter, Gmail, Slack, Telegram, Google Sheets, HTTP Request, Postgres) |
| `BUILD_SYSTEM_PROMPT` | Ground every node in live tools; never invent params/credentials; empty credential placeholders; `write_json_file` exactly once |
| `confirm_node` | `interrupt({"type": "approval_request", "summary": ...})`. `approved` → goto `build`; else loop back to `plan` with the feedback as a `HumanMessage` |
| `build_node` | Binds the **7 curated not-management tools** (`tools_documentation`, `search_nodes`, `get_node`, `validate_node`, `search_templates`, `get_template`, `validate_workflow`) + a sandboxed `write_json_file`. Per-build `tempfile.mkdtemp(prefix="botchain-build-")`; `write_json_file` enforces a sandbox containment check (no path escape). `_NodeLookupCache` dedupes `search_nodes`/`get_node`/`tools_documentation` in-memory. `MAX_TOOL_LOOP_TURNS=30`. Fallback: parse raw JSON from the model's final message if it skipped `write_json_file` |
| `validate_node` | `build_workflow_with_validation`: call n8n's `validate_workflow`, self-repair via `_repair_workflow` up to `MAX_VALIDATION_RETRIES=3`; then graph-level `MAX_GRAPH_RETRIES=3`. Never silently fails — best-effort JSON + remaining errors on non-convergence |
| Status heartbeats | Nodes push custom stream events (`Planning…`, `Looking that node up…`, `Assembling the workflow file…`, `Checking the workflow…`, `Fixing validation errors…`) to keep the connection alive during long build/validate passes |

### 1.2 Route wiring — `src/app/api/v1/messages.py` (430 lines)

- **`POST {chat_id}/messages`** → persists the user row, then `StreamingResponse` over
  `_assistant_stream`: consumes `stream_mode=["messages","custom"]`; message chunks →
  plain text chunks (Text Stream Protocol), custom statuses forwarded but not stored.
- **`POST {chat_id}/approve`** → `_approve_stream` resumes the interrupted graph with
  `Command(resume={"approved":..., "feedback":...})` and streams the continuation
  (build+validate on approval, replan on rejection). Deliberate **409** if no pending
  approval (Frontend reconciliation: pending state is signalled via `Message.meta`,
  there is no `status` column).
- **Per-chat locks** `_chat_locks` serialise sends + approvals for the same thread;
  released in the generator's `finally` (the request task is gone by the time the
  StreamingResponse body runs).
- **`_run_meta`** derives the terminal `Message.meta`: `phase`, `approval: {status}`,
  `workflow_json`, `workflow_name`, `validation{status, errors}`; `is_error=True` for
  failed/completed-with-errors runs so the frontend can render failure clearly.
- **Persistence integrity** (`_final_assistant_text`): the persisted assistant row comes
  from the **final committed state message**, never from joining the stream buffer
  (see §3 bugs found).
- **Token accounting** — `_chunk_usage` sums `usage_metadata` off the streamed chunks.
  Ollama populates usage only on each generation's *final* chunk, so summing across the
  run (plan structured call, build tool-loop, repair passes) gives the exact per-run
  totals. Persisted to the triggering user row (`input_tokens`, `output_tokens=0`,
  backfilled post-run) and the assistant row (`input_tokens` + `output_tokens`).
  Exposed on `MessageOut`.
- **Parent chaining** — the schema/API always accepted `parent_id` on `MessageCreate`,
  but nothing populated it before. Now every assistant reply is linked to the user
  message that triggered it (`parent_id = user_message_id`) and the approve continuation
  links to the pending confirm message (`parent_id = pending.id`). Message-level lineage
  (and the ability to fork a user message off any earlier point in the chat) now works;
  agent-level thread forking is still linear per chat (see §5).
- **Attachment creation** — on `phase=done` with a `workflow_json`, `_flush_assistant_row`
  creates an `Attachment` row extracted from the final message's meta:
  `file_name` (`workflow_name` or `workflow.json`), `file_type=application/json`,
  `size_bytes`, and `file_url` pointing at a new download route
  `GET {chat_id}/messages/{message_id}/attachments/{attachment_id}/download` that streams
  the JSON straight from `Message.meta` (DB-persisted, survives the ephemeral Railway
  disk — no object storage needed yet). `MessageOut.attachments` lists them.

### 1.3 Interfaces

- `src/app/services/llm.py` — single `create_model()` factory (swap provider = one file).
- `src/app/main.py` — lifespan builds the n8n-mcp `MultiServerMCPClient` stdio config,
  binds tools, constructs the compiled graph, stores it on `app.state.agent`; shutdown
  nulls `app.state.mcp_client` (each tool call spawns its own npx stdio subprocess —
  nothing to close; `__aexit__` raises `NotImplementedError`).
- Management tools (`n8n_*`, need a live n8n instance) are deliberately excluded from
  the build surface.

---

## 2. Bugs found & fixed during verification (real-stack, not theory)

1. **`json_schema` structured output is not enforced by Ollama Cloud.** `nemotron-3-ultra:cloud`
   returned wrapped/prose output → "Failed to parse Plan from completion". Fix:
   `with_structured_output(PlanOutput, method="function_calling")`.
2. **The model can return *nothing at all* (empty structured output).** With long
   dialog history it occasionally produced no tool call at all, twice in a row — which
   crashed the graph. Final fix in `_invoke_structured_with_retry`: structured attempt →
   retry with an explicit "return ONLY strict JSON" system prompt that **embeds the
   `PLAN_SCHEMA` JSON schema** → plain-completion fallback parsed from raw JSON text.
3. **`stream_mode="messages"` surfaces *internal* model calls.** Build/validate repair
   passes emit the raw workflow JSON as streamed tokens. Joining the buffer would have
   persisted raw JSON into the user's assistant row. Fix: persist from the committed
   state message (`_final_assistant_text`), keep the buffer only as an exception-path
   fallback.
4. **MCP `__aexit__` raises `NotImplementedError`** (langchain-mcp-adapters quirk).
   Shutdown just nulls the client reference — verified it's safe because every tool call
   spawns a fresh npx stdio process.
5. **Confirm-flush precedence bug (2026-09-25, found in HTTP e2e).** The
   `context_summary` branch of `_flush_assistant_row` was written
   `await session.execute(...).scalar_one_or_none()` — `await` binds to the *result* of
   `.scalar_one_or_none()`, so the `execute()` **coroutine** got the attribute
   (`AttributeError: 'coroutine' object has no attribute 'scalar_one_or_none'`). It
   only executes when `meta.phase == "confirm"` (to set `Chat.context_summary`), which
   Phase 4's mock agent never emitted — the first real confirm turn crashed the flush,
   **dropped the pending-approval Message row** and **skipped `lock.release()`** (chat
   permanently wedged: both `/messages` and `/approve` would 409). Fixes:
   `(await session.execute(...)).scalar_one_or_none()`, and both stream-generator
   `finally` blocks now wrap the flush in a nested `try/finally` so the lock is always
   released. Regression tests added in `tests/test_message_persistence.py` (2), incl.
   one asserting the Chat-row update path actually runs.

---

## 3. Tests performed & outputs

### 3.1 Unit / regression tests (pytest) — 38 passing, ruff clean

| File | Tests | Covers |
|---|---|---|
| `tests/test_security.py` | 18 | Kinde JWKS verification (minted RS256 JWTs vs mocked JWKS), read-only `current_user` |
| `tests/test_checkpoint.py` | 3 | `AsyncPostgresSaver` pool factory, checkpoint tables, Alembic exclusion |
| `tests/test_message_flush.py` | 2 | `_fire_and_forget` background-flush semantics (strong reference, failure logging) |
| `tests/test_message_persistence.py` | 10 | `_final_assistant_text` (4); `_flush_assistant_row` confirm path sets `Chat.context_summary` + no-summary path skips the Chat query (2, regression for bug #5); `_chunk_usage` parsing incl. metadata-less None (2); attachment creation on `phase=done` with `workflow_json` + skipped without a workflow (2) |
| `tests/test_plan_fallback.py` | 5 (new, Phase 5) | `_invoke_structured_with_retry`: plain-JSON fallback parse, code-fence stripping, structured short-circuit, schema embedded in prompt, raises on invalid JSON |

```
$ uv run pytest -q
......................................                                      [100%]
```

### 3.2 Real-model probes (Ollama Cloud, no fake)

**Structured-output reliability probe — 3/3:**
```
attempt 0 OK goal='Post contact form submissions (name + me' ready=False
attempt 1 OK goal='Post contact form submissions (name and ' ready=False
attempt 2 OK goal='Post contact form submissions (name and ' ready=False
successes: 3 /3
```

**Forced-fallback probe** (structured path made to raise every time → plain-JSON
fallback must recover):
```
FALLBACK OK: goal='Receive contact form submissions via webhook and post them t' ready=True services=['Slack'] trigger=Webhook
PASS
```

### 3.3 End-to-end real gate — `notebooks/testcases.md` → validated workflow

Full graph driven with **real Ollama + real n8n-mcp tools** (28 MCP tools loaded) + real
Postgres checkpointer. Prompt: *"Whenever someone submits my contact form, post the
submitter's name and message to our #general Slack channel."*

**Planning phase (2 turns) → reached confirm:**
```
[status] Planning…
--- send: 'Use a Webhook trigger at path /contact. Post the submitter name and message to the #general channel on Slack.'
[status] Planning…
meta: {'phase': 'confirm', 'approval': {'status': 'pending'},
       'spec': {'goal': 'Post contact form submissions (name + message) to #general Slack channel',
                'trigger_type': 'webhook',
                'services_involved': ['Slack'],
                'conditions_logic': 'None',
                'data_flow': 'Contact form submission -> Extract name and message -> Post to Slack #general',
                'constraints': 'None',
                'open_questions': []}}
  >>> paused at approval interrupt
```

**Approval → build → validate (self-repaired twice, validated on 3rd attempt):**
```
[status] Looking that node up…  (×4)
[status] Assembling the workflow file…
[status] Checking the workflow…
[status] Fixing validation errors…
[status] Looking that node up… / Assembling / Checking…
[status] Fixing validation errors…
[status] Looking that node up… / Assembling / Checking…
[status] Workflow validated. Done.
```

**Terminal meta + deliverable:**
```
FINAL meta: {'phase': 'done', 'validation': {'status': 'valid', 'errors': []},
             'workflow_name': 'workflow.json'} is_error: False
WORKFLOW OK: Contact Form to Slack
nodes: ['Webhook', 'Post to Slack']
```

**Validated workflow JSON** (key nodes, real n8n types, empty credential placeholder):
```json
{
  "name": "Contact Form to Slack",
  "nodes": [
    { "id": "webhook", "name": "Webhook",
      "type": "n8n-nodes-base.webhook", "typeVersion": 2.1,
      "parameters": { "httpMethod": "POST", "path": "contact", "responseMode": "onReceived" },
      "position": [250, 300] },
    { "id": "slack", "name": "Post to Slack",
      "type": "n8n-nodes-base.slack", "typeVersion": 2.7,
      "credentials": { "slackApi": { "id": "", "name": "Slack account" } },
      "parameters": { "channelId": { "mode": "list", "value": "general" },
                      "operation": "sendAndWait",
                      "text": "=New contact form submission:\n*Name:* {{ $json.name }}\n*Message:* {{ $json.message }}" },
      "position": [550, 300] }
  ],
  "connections": { "Webhook": { "main": [[{ "node": "Post to Slack", "type": "main", "index": 0 }]] } },
  "settings": {}, "active": false
}
```

### 3.4 Concurrent-build isolation smoke (delta 1)

Two chats streamed the agent simultaneously (stub model, monkeypatched flush — no DB
writes). Verifies the per-chat lock holds across the whole stream and each chat persists
only its own workflow:

```
FLUSH chat-A: is_error=False meta.phase=done
FLUSH chat-B: is_error=False meta.phase=done
A chunks: ['status-A-0', '[A token 0]', 'status-A-1', '[A token 1]', 'status-A-2', '[A token 2]']
B chunks: ['status-B-0', '[B token 0]', 'status-B-1', '[B token 1]', 'status-B-2', '[B token 2]']
A contamination ('[B token' in A): False
B contamination ('[A token' in B): False
```

### 3.5 Real HTTP + Neon DB-persistence e2e (2026-09-25)

Full stack through the actual routes — uvicorn (`app.main:app`) + real Kinde JWT +
Ollama Cloud + n8n-mcp + live Neon (`billowing-snow-05570527`, production branch). Chat
created with title **`db-persistence-test-ollama-model-e2e`** (post-bug-fix run:
`c136f1bd-…`; the first run hit bug #5, was cleaned up via soft-delete).

```
POST /chats                          → 201, id c136f1bd-ab83-45e6-87b7-3f50f38194a7
POST /chats/{id}/messages  #1         → "Planning… Got it — … how does your contact form send data?"
POST /messages  #2 ("It POSTs to a webhook URL. Use a Webhook trigger at path /contact.")
                                     → "Planning… Perfect — webhook trigger at /contact it is. What field names…?"
POST /messages  #3 ("The JSON fields are name and message.")
                                     → "Planning… Here's what I'll build: … Should I build this now?"
POST /chats/{id}/approve {"approved":true}
                                     → build + validate stream → "passed n8n validation after 3 attempt(s)"
```

**Verified in Neon after the run** (7 `messages` rows, ordered):

| role | content (head) | meta |
|---|---|---|
| user | "Whenever someone submits my contact form…" | `{}` |
| assistant | "Got it — when a contact form is submitted…" | `{"phase": "plan"}` |
| user | "It POSTs to a webhook URL…" | `{}` |
| assistant | "Perfect — webhook trigger at `/contact` it is…" | `{"phase": "plan"}` |
| user | "The JSON fields are name and message." | `{}` |
| assistant | "Here's what I'll build…" | `{"phase": "confirm", "approval": {"status": "approved"}, "spec": {…}}` |
| assistant | "Your workflow is ready to import…" | `{"phase": "done", "validation": {"status": "valid", "errors": []}, "workflow_name": "workflow.json", "workflow_json": {…}}` |

- User rows are written **synchronously before streaming** each turn (the handler
  commits before returning the `StreamingResponse`); assistant rows are flushed by the
  generator's `finally` after the stream completes.
- The confirm row was `approval.status=pending` after the planning turn and mutated to
  `approved` by `/approve` — this is the row the old bug #5 dropped.
- The chat row for this e2e carries `context_summary` = the confirm summary (set by the
  `context_summary` branch of `_flush_assistant_row`).
- `is_error=False` on every row; workflow validated (2-node Webhook + Slack, empty
  credential placeholder), `workflow_json` persisted to meta.

### 3.6 Prompt-modularization verification (2026-09-25)

Prompts moved from `services/agent.py` into a `src/app/prompts/` package (`plan.py`,
`build.py`, `repair.py`, `constants.py`; ship-able via `packages = ["src/app"]`). Root
`prompts/` remains a prototype-only placeholder (`prototype.py` still imports it; it
references tools that don't exist in prod — single-agent prompt vs the graph). Guardrails
§7–§10 + the secrets warning are now inside `plan.py`/`build.py` templates, not just
AGENTS.md. Real-model probe against the new packaged prompt:

```
PROBE OK goal='Post contact form submissions (name + message) to a #general Slack channel'
      ready=False services=['Slack']   # planner still interviews one question at a time
```

### 3.7 Token-usage, parent-chaining & attachment e2e (2026-09-25)

Real stack through the routes (uvicorn + real Kinde JWT + Ollama Cloud + n8n-mcp + live
Neon). Chat `155a3233-0ca3-4f9c-803f-dc18464d1667` ran the same contact-form prompt to a
validated Webhook → Slack workflow. **Every `messages` row after the run** (ordered):

| role | parent_id | input_tokens | output_tokens | meta phase | attachments |
|---|---|---|---|---|---|
| user | `None` | 1117 | 0 | — | `[]` |
| assistant | `308098ca…` (user #1) | 1117 | 379 | plan | `[]` |
| user | `None` | 4349 | 0 | — | `[]` |
| assistant | `1cb9f6eb…` (user #2) | 4349 | 783 | plan | `[]` |
| user | `None` | 1379 | 0 | — | `[]` |
| assistant | `0d85e0c2…` (user #3) | 1379 | 340 | confirm | `[]` |
| assistant | `07abc641…` (confirm msg) | 95717 | 8413 | done | `[df93e33f…: workflow.json]` |

- **Tokens:** each user row carries the run's input (output 0); each assistant row carries
  input + output. Large numbers on the `done` row (95k in / 8.4k out) are the summed
  build tool-loop + repair generations, not a single model call.
- **Parents:** the three plan/confirm replies chain to their triggering user messages; the
  `done` continuation chains to the pending confirm message (`07abc641…`) — lineage is
  complete for the linear flow.
- **Attachment:** the `done` row has one `Attachment` (`file_name=workflow.json`,
  `file_type=application/json`, `size_bytes=755`, `file_url` = download route). Fetching
  that URL returned:
  ```
  HTTP/1.1 200 OK
  content-disposition: attachment; filename="workflow.json"
  content-type: application/json
  ```

---

## 4. Guardrails honored (inherited system prompt)

- No node type/parameter/credential field fabricated — every node grounded in
  `search_nodes`/`get_node`, only retrieved properties used.
- No real secrets in workflow JSON — Slack credential is an empty `id:""` placeholder.
- Never "done" without passing n8n validation **and** explicit human approval — both
  gates enforced in the graph/route.
- Validation retries capped at 3; non-convergence → best-effort JSON + clear remaining
  issues + rename request, never a silent failure.
- Token usage is read from the provider's streamed `usage_metadata` — never guessed,
  synthesized, or pulled from model memory.
- Only an **explicitly approved, validated** final workflow becomes an `Attachment` (the
  `done` phase gate) — approval + validation both required before a downloadable file
  exists.

## 5. Known follow-ups (Phase 6+)

- Client still *sees* internal tool/JSON fragments in the raw token stream; only the
  persisted row is cleaned. Tab later to the Data Stream Protocol for
  tool-call/reasoning indicators.
- Detached-process smoke scripts die when their stdin closes in a throwaway shell —
  artifact of this sandbox, not the app (served via uvicorn it behaves normally).
- HTTP-route e2e runs against the **production** Neon branch (there is no separate test
  DATABASE_URL wired to the app env); Phase 8's `conftest.py` on a `tests` Neon branch is
  the place to make e2e/test DBs separable.
- Attachment `file_url` points at the backend download route (DB-served from
  `Message.meta`) while there's no object storage; swap to a real S3/R2 object URL when
  storage lands (Phase 7 / Phase 9 infra).
- Message-level lineage works, but **agent-level thread forking is not implemented** —
  each chat runs one linear LangGraph thread (`thread_id = chat_id`); a user message with
  a `parent_id` mid-history is stored with that parent but the graph still runs linearly.
  Real branch/resume-per-thread would need multiple thread_ids per chat.