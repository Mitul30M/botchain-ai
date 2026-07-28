# BotChain AI — Prototype Architecture Guide

This document explains how `prototype/prototype.py` works end-to-end. No source files are modified by this document.

---

## 1. Overview

`prototype/prototype.py` is a self-contained terminal prototype that wires together:

- **n8n-mcp** — a Model Context Protocol server exposing live n8n node documentation and a workflow validator.
- **LangGraph / Deep Agents** — an agent framework with streaming, checkpointing, and human-in-the-loop interrupts.
- **Ollama** — a local LLM provider (nemotron-3-ultra) used for reasoning, tool-calling, and JSON generation.
- **Rich console** — token-by-token streaming output with color-coded sections for reasoning, tool calls, and approval interrupts.

The prototype lets a user describe an automation need in plain English and receive a validated n8n workflow JSON file, written to a sandboxed directory, with a human approval gate before any file is persisted.

---

## 2. Running the Prototype

```bash
uv run python -m prototype.prototype
```

The module path `prototype.prototype` resolves because `prototype/` is a Python package (`__init__.py` exists).

---

## 3. Directory Structure (Created at Startup)

| Path | Purpose |
|---|---|
| `sandbox/` | Write-only sandbox where workflow JSON files are saved. Path-traversal is blocked. |
| `store/` | Persistence directory containing the SQLite checkpoint DB. |
| `store/checkpoints.sqlite` | LangGraph checkpoint database — enables resuming interrupted sessions. |

All three directories are created automatically if missing.

---

## 4. Environment Variables

Loaded from `.env` via `python-dotenv`. Key variables:

| Variable | Default | Purpose |
|---|---|---|
| `LANGSMITH_TRACING` | `"true"` | Enables LangSmith trace export. |
| `LANGSMITH_API_KEY` | *(none)* | LangSmith API key for tracing. |
| `LANGSMITH_ENDPOINT` | `"https://api.smith.langchain.com"` | LangSmith endpoint. |
| `LANGSMITH_PROJECT` | `"botchain-ai"` | LangSmith project name. |
| `OLLAMA_API_KEY` | *(none)* | Bearer token for the Ollama API. |
| `N8N_API_URL` | *(none)* | URL of the n8n instance the MCP server connects to. |
| `N8N_API_KEY` | *(none)* | API key for authenticating with n8n. |

---

## 5. Component Initialization (async)

`setup_agent()` is the one-time async initializer. It is called once before any chat interaction and populates four module-level globals: `mcp_tools`, `model`, `checkpointer`, and `agent`.

### 5.1 MCP Client (n8n-mcp)

A `MultiServerMCPClient` connects to `n8n-mcp` via **stdio** transport:

```
npx n8n-mcp    (runs locally as a subprocess)
```

The MCP session is opened with `mcp_client.session("n8n-mcp")` and tools are loaded with `load_mcp_tools(mcp_session)`. These tools include things like `validate_workflow`, `search_nodes`, `get_node_documentation`, etc. — the full set depends on the running n8n-mcp installation.

### 5.2 LLM (Ollama)

A `ChatOllama` instance is created with:

- **Model**: `nemotron-3-ultra:cloud`
- **Base URL**: `https://ollama.com`
- **Temperature**: `0.2` (low — deterministic, structured output)
- **Auth**: Bearer token from `OLLAMA_API_KEY`

### 5.3 Checkpointer (SQLite)

An `AsyncSqliteSaver` backed by `aiosqlite` connects to `store/checkpoints.sqlite`. This enables LangGraph to persist checkpoint state between runs so a user can resume a session mid-plan without losing progress.

### 5.4 Deep Agent

`create_deep_agent()` from the `deepagents` library combines the model, tools, system prompt, filesystem backend, and checkpointer into a single agent graph.

---

## 6. Agent Tools

The agent receives three categories of tools:

### 6.1 n8n MCP Tools (dynamic)

Loaded from the MCP server at runtime. Their exact names and schemas depend on the installed n8n-mcp version. The prototype assumes tools like `validate_workflow` exist and adapts to them.

### 6.2 `write_json_file` (custom)

Writes a JSON-serializable dict or list to a file inside the `sandbox/` directory.

**Safety guards**:
- Strips leading `/` and `sandbox/` prefixes from the file path.
- Resolves the target path and verifies it is inside the sandbox root (prevents path traversal).
- Creates parent directories automatically.
- Overwrites existing files.

**Schema**:
| Field | Type | Description |
|---|---|---|
| `file_path` | `str` | Relative path within sandbox |
| `content` | `dict \| list` | JSON-serializable data |

### 6.3 `build_workflow_with_validation` (custom)

The central "build and verify" tool. It wraps workflow construction with automatic validation and self-repair.

**Flow**:
1. Call `validate_workflow` from the MCP tools with the current workflow JSON.
2. If `valid: true`, return the result.
3. If validation fails, ask the LLM to **repair only the reported errors** (not rewrite the whole workflow).
4. Repeat up to **3 retries**.
5. Return a result dict with `status` of either `"valid"` or `"failed_after_retries"`, along with diagnostics.

**Key helper functions used internally**:
- `_strip_code_fences()` — strips ` ```json ` markdown fences from model output.
- `_extract_json_from_mcp_result()` — normalizes MCP returns (str, dict, or list of content blocks) into a dict.
- `_call_validate()` — tolerates argument-name variations across MCP tool versions.
- `_repair_workflow()` — sends errors + current JSON back to the LLM and asks for a corrected full JSON.

**Schema**:
| Field | Type | Description |
|---|---|---|
| `workflow_name` | `str` | Kebab-case slug |
| `workflow_json` | `dict` | Full n8n workflow JSON |

### 6.4 `request_human_approval` (custom)

A **synchronous** tool that calls `interrupt()` from LangGraph. This pauses agent execution and surfaces a summary to the user.

**What it shows the user**:
- Workflow name (slug)
- List of nodes that will be created
- Credential types referenced
- External services the workflow touches
- Plain-language summary of what the workflow does

**Behavior**:
- If the user approves → returns `"APPROVED by user. Proceed to write the workflow file..."` and execution continues.
- If the user rejects → returns `"REJECTED by user..."` and the agent loops back to planning/building.

---

## 7. Agent State & Graph

The agent runs as a LangGraph `StateGraph` (managed internally by `deepagents`). The shared state includes:

- `messages` — full chat history (`BaseMessage` objects)
- `phase` — current phase indicator (planning, building, validating, done)
- Any custom state fields the system prompt and tools produce

Checkpointing is provided by `AsyncSqliteSaver`, so the agent's state is persisted at each step. This enables:
- **Session resume** — reconnect to `thread_id` and continue from where the agent left off.
- **Interrupt recovery** — after a human approval decision, the agent resumes from the exact point it was paused.

---

## 8. Chat Flow

### 8.1 Normal Message

1. User types a message in the terminal.
2. `chat()` streams the agent's response token-by-token via `agent.astream()`.
3. The `_stream_response()` coroutine renders output in real time using `rich.Live`:
   - **Reasoning blocks** (thinking/reasoning type) — printed immediately as they arrive.
   - **Text content** — streamed into a live-updating panel.
   - **Tool calls** — only the tool name is printed (not verbose args).
   - **Tool messages** — suppressed (MCP results can be thousands of lines).
4. After streaming completes, the handler checks for an approval interrupt.
5. If an interrupt is pending, the user is prompted: `approve> `.

### 8.2 Approval Interrupt

When `request_human_approval` is called by the agent, execution pauses. The user sees:

```
⏸  APPROVAL NEEDED
  (summary panel with nodes, credentials, services)

Call: approve(thread_id="...", approved=True)  # or approved=False
```

The user types:
- `approve` → calls `approve(thread_id, True)` which resumes with `Command(resume={"approved": True})`
- Any other text → treated as rejection feedback; `approve(thread_id, False, feedback=...)`

### 8.3 Terminal Chat Loop

`main()` runs an `asyncio` event loop with a simple `while True` loop:
- Prompts `You> ` for input.
- Calls `chat()` for each message.
- Exits on `quit`, `exit`, `q`, `EOFError`, or `KeyboardInterrupt`.

---

## 9. System Prompt

The system prompt is imported from `prompts.system_prompt` (a separate module). It defines the agent's role, behavior, and phase-transition logic (Plan → Confirm → Build → Validate → Done). Since this file is not part of `prototype.py`, its contents are not included here — but it is the "brain" that orchestrates the agent's decision-making.

---

## 10. Key Design Decisions

| Decision | Rationale |
|---|---|
| **stdio MCP transport** | Self-hosted `n8n-mcp` as a subprocess — no external network dependency for the MCP server. |
| **Sandbox file writes** | `write_json_file` prevents path traversal so the agent cannot escape `sandbox/`. |
| **Self-repair loop** | Letting the model fix its own validation errors is cheaper and faster than manual debugging, capped at 3 retries. |
| **Human approval gate** | `interrupt()` is a LangGraph primitive — no custom state tracking needed. It also works with `deepagents` out of the box. |
| **SQLite checkpointer** | Near-zero setup; the agent can be resumed after a crash or browser close (in a future web app). |
| **Rich console for streaming** | Color-coded sections (reasoning, tool calls, interrupts, responses) make the terminal output readable even during long agent runs. |
| **Low temperature (0.2)** | Structured JSON output (workflow files) benefits from deterministic, low-creativity generation. |

---

## 11. Data Flow Diagram

```
User (terminal)
   │
   ▼
chat() ──► agent.astream() ──► _stream_response()  [Rich Live console]
   │                                    │
   │                         ┌──────────┴──────────┐
   │                         │   Agent Graph (LangGraph)  │
   │                         │  ┌─────────────────────┐  │
   │                         │  │  System Prompt      │  │
   │                         │  │  + MCP tools        │  │── validate_workflow (n8n-mcp)
   │                         │  │  + write_json_file  │  │
   │                         │  │  + build_workflow   │  │── self-repair loop
   │                         │  │    with validation  │  │
   │                         │  │  + request_approval │  │── interrupt() gate
   │                         │  └─────────────────────┘  │
   │                         └──────────┬────────────────┘
   │                                    │
   ▼                                    ▼
approve() ◄────── interrupt() ◄────── agent pauses
   │                                    (on approval)
   ▼
agent.astream(Command(resume=...))  ──►  continues graph execution
```

---

## 12. Extending the Prototype

Possible enhancements (none of which modify `prototype.py`):

- **Web frontend** — replace the terminal chat loop with a FastAPI + SSE endpoint.
- **Eval fixtures** — a `tests/` directory with 5–10 prompt → expected workflow shape pairs for regression testing.
- **Node surface scoping** — add a allowlist of ~15–20 nodes in the system prompt to bound demo risk.
- **MCP call caching** — wrap `search_nodes` and `get_node_essentials` with an in-memory LRU cache to reduce latency during live demos.
- **Postgres checkpointer** — swap `AsyncSqliteSaver` for `PostgresSaver` for multi-user production deployments.