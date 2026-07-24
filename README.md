Good stack choice — LangGraph is actually a strong fit here because Plan/Build maps naturally onto a graph with distinct nodes/states, and you get built-in support for interrupts (pausing between Plan and Build for user confirmation) and checkpointing (conversation persistence) for free. Here's a detailed backend plan.

## 1. Tech stack (backend)

| Layer | Choice | Why |
|---|---|---|
| Package/env mgmt | `uv` | You've already decided this — fast, clean lockfile |
| Web framework | FastAPI | Async-native, pairs well with LangGraph streaming, easy OpenAPI docs |
| Agent orchestration | LangGraph | State machine semantics fit Plan→Build; built-in persistence/checkpointing |
| LLM | Claude (Anthropic API) via `langchain-anthropic` | You're already in the Claude ecosystem; good tool-use reliability |
| MCP integration | `langchain-mcp-adapters` (official LangChain MCP client) | Converts MCP tools (from n8n-mcp) into LangChain-compatible tools with minimal glue code |
| Session/state persistence | LangGraph checkpointer — start with `SqliteSaver`, move to `PostgresSaver` later | Lets a user close the tab mid-plan and resume |
| Structured output | Pydantic models + LangChain's `.with_structured_output()` | Needed for the requirements spec and for forcing well-formed n8n JSON |
| Streaming to frontend | SSE (`sse-starlette`) or WebSockets | Chat UIs feel broken without token streaming |
| Validation | n8n-mcp's `validate_workflow` tool + your own JSON Schema check as a second pass | Defense in depth before handing the file to the user |

## 2. Project structure

```
n8n-agent-backend/
├── pyproject.toml
├── app/
│   ├── main.py                 # FastAPI app, routes
│   ├── api/
│   │   ├── chat.py             # POST /chat, /chat/stream (SSE)
│   │   └── sessions.py         # session CRUD, resume, download workflow
│   ├── graph/
│   │   ├── state.py            # Pydantic state schema for the graph
│   │   ├── graph.py            # LangGraph StateGraph definition (nodes + edges)
│   │   ├── nodes/
│   │   │   ├── plan.py         # Plan-phase node(s)
│   │   │   ├── confirm.py      # Human-in-the-loop confirmation / interrupt
│   │   │   ├── build.py        # Build-phase agent loop
│   │   │   └── validate.py     # Validation + self-correction loop
│   │   └── mcp_client.py       # n8n-mcp connection + tool loading
│   ├── models/
│   │   ├── requirements.py     # Pydantic: RequirementsSpec
│   │   └── workflow.py         # Pydantic: minimal n8n workflow shape (for pre-checks)
│   ├── services/
│   │   ├── storage.py          # session persistence (sqlite/postgres)
│   │   └── export.py           # writes final .json, returns download path
│   └── config.py               # env vars, settings via pydantic-settings
├── tests/
└── .env.example
```

## 3. Core state design

This is the most important design decision — get the shared state object right and everything else follows.

```python
class RequirementsSpec(BaseModel):
    goal: str
    trigger_type: str | None        # webhook, schedule, manual, form, etc.
    services_involved: list[str]    # ["Gmail", "Slack", "Google Sheets"]
    conditions_logic: str | None    # plain-language description of branching
    data_flow: str | None           # what moves from where to where
    constraints: list[str] = []     # rate limits, auth notes, etc.
    open_questions: list[str] = []  # things still unclear

class AgentState(BaseModel):
    messages: list[BaseMessage]              # full chat history
    phase: Literal["plan", "confirm", "build", "validate", "done"]
    spec: RequirementsSpec | None = None
    workflow_json: dict | None = None
    validation_errors: list[str] = []
    retry_count: int = 0
```

`phase` is your explicit mode switch — the frontend can read it to show "Planning…" vs "Building…" indicators, and it's also what routes the LangGraph edges.

## 4. Graph flow

```
START → plan_node ⇄ (loops with user until spec is "complete enough")
              ↓
        confirm_node (interrupt: "Here's what I understood — build it?")
              ↓ (user confirms)
        build_node → validate_node → (pass) → END
                          ↓ (fail, retry_count < N)
                     build_node (self-correct with validation errors fed back in)
```

Key mechanics:
- **plan_node**: a chat loop that also tries, each turn, to fill in `RequirementsSpec` via structured output. Once all required fields are non-null and there are no `open_questions`, it proposes moving to confirm.
- **confirm_node**: uses LangGraph's `interrupt()` to pause execution and show the user a plain-English summary of the spec before touching Build — cheap insurance against building the wrong thing.
- **build_node**: an agent loop with n8n-mcp tools bound (`search_nodes`, `get_node_essentials`, `get_node_documentation`, etc.), synthesizing the workflow JSON from the spec + tool results.
- **validate_node**: calls `validate_workflow` (n8n-mcp) and/or your own schema check; if it fails, route back to build_node with the errors appended to state so the model can self-correct. Cap retries (e.g. 3) so it doesn't loop forever — surface remaining errors to the user if it can't converge.

## 5. MCP integration specifics

`langchain-mcp-adapters` gives you something like:

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

client = MultiServerMCPClient({
    "n8n": {
        "transport": "stdio",  # or "sse"/"http" if using hosted n8n-mcp
        "command": "npx",
        "args": ["n8n-mcp"],
    }
})
tools = await client.get_tools()
```

Bind `tools` to your build-phase LLM call. One decision to make early: **self-host n8n-mcp locally in your container vs. use their hosted version** — self-hosting is more reliable for a demo (no external dependency going down mid-presentation) but needs the `n8n-nodes-base` package available, which adds build time. Self-hosted stdio is probably your safest bet for a judged demo.

## 6. API surface (minimal viable)

```
POST /sessions                    → create new session, returns session_id
POST /sessions/{id}/chat          → send a message, returns agent reply + phase
GET  /sessions/{id}/chat/stream   → SSE stream of the above (for token-by-token UI)
GET  /sessions/{id}/spec          → current RequirementsSpec (for a "requirements" side panel)
POST /sessions/{id}/confirm       → resumes graph from confirm interrupt
GET  /sessions/{id}/workflow      → download final validated .json
GET  /sessions/{id}/history       → full message history (for resume)
```

## Recommendations / things to account for

**Scope the node/service surface deliberately.** n8n-mcp covers 1,600+ nodes, but for a project demo you don't need all of them to work — decide on ~15-20 well-tested nodes (Webhook, HTTP Request, Set, IF/Switch, Code, Schedule Trigger, Gmail, Slack, Google Sheets, Telegram) and mention in your plan-phase prompt that the agent should prefer these, falling back to others only if necessary. This bounds your testing surface and demo risk.

**Guard against credential/secrets leakage in output.** The agent will generate nodes needing OAuth/API credentials — make sure the exported JSON never contains actual secret values, only credential *placeholders* (n8n handles credentials separately from workflow JSON anyway, but double check your prompt doesn't ask the model to fabricate example tokens).

**Add a "critic" pass, not just schema validation.** Schema validity ≠ logical correctness. Consider a lightweight node in the graph that re-reads the spec vs. the generated workflow and flags mismatches ("spec says Slack DM on failure, workflow only has success path") before showing the file to the user. Cheap to add, meaningfully improves output quality for your demo.

**Rate-limit / cache MCP calls.** `search_nodes` and `get_node_essentials` will get called repeatedly for common nodes across sessions — an in-memory or Redis cache keyed by node name saves latency and API calls during a live demo.

**Decide now: streaming granularity.** Users will find "thinking silently for 20 seconds during Build" bad UX. Stream intermediate status ("Searching for Slack node…", "Validating workflow…") even if you're not streaming raw tokens for that phase — LangGraph's `astream_events` gives you this for free if you tap into node-level events.

**Plan for graceful validation failure.** Sometimes the agent won't converge in 3 retries. Have a defined fallback: return the best-effort JSON *plus* a clear list of remaining issues and manual fix instructions, rather than silently failing. Judges will appreciate seeing you handled the unhappy path.

**Write a handful of eval fixtures early.** 5-10 fixed (prompt → expected workflow shape) pairs you can run through the pipeline as a smoke test after any change. Saves you from "it worked yesterday" surprises right before a demo.

**Session persistence matters more than it seems.** If your judged demo involves any live back-and-forth, a browser refresh shouldn't lose state — `SqliteSaver` checkpointing is nearly free to wire up now and saves you from a bad live moment later.

Want me to scaffold the actual `pyproject.toml` + skeleton files next, or go deeper on any one node (e.g. the build_node agent loop logic) first?