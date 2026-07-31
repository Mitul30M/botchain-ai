# to run: uv run python -m prototype.prototype
import os
import asyncio
import json
import re
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

# --- LangSmith tracing ---
os.environ["LANGSMITH_TRACING"] = os.getenv("LANGSMITH_TRACING", "true")
os.environ["LANGSMITH_API_KEY"] = os.getenv("LANGSMITH_API_KEY")
os.environ["LANGSMITH_ENDPOINT"] = os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
os.environ["LANGSMITH_PROJECT"] = os.getenv("LANGSMITH_PROJECT", "botchain-ai")
os.environ["OLLAMA_API_KEY"] = os.getenv("OLLAMA_API_KEY")
os.environ["N8N_API_URL"] = os.getenv("N8N_API_URL")
os.environ["N8N_API_KEY"] = os.getenv("N8N_API_KEY")

# --- Rich console setup ---
from langchain_mcp_adapters.client import MultiServerMCPClient
from rich.console import Console
from rich.text import Text
from rich.panel import Panel
from rich.live import Live
from rich.markdown import Markdown
from rich.rule import Rule

# Color palette per spec — adjusted for readability on light/dark backgrounds
COLORS = {
    "reasoning": "#b4a8d6",
    "tool_called": "#a89cc4",
    "interrupt": "#b83d5e",
    "interrupt_dark": "#9a2d4a",
    "response": "#a6adc8",
    "user": "#89b4fa",
    "botchain": "#cba6f7",
    "separator": "#6c7086",
}

console = Console()


def print_separator(char="─", style="separator"):
    console.print(char * 60, style=COLORS[style])


def print_header(label: str, style: str):
    console.print(f"\n{label}:", style=f"bold {COLORS[style]}")


def print_reasoning(text: str):
    console.print(Text("Reasoning: ", style=f"bold {COLORS['reasoning']}") + Text(text, style=COLORS['reasoning']))


def print_tool_called(tool_name: str):
    console.print(Text("Tool Called: ", style=f"bold {COLORS['tool_called']}") + Text(tool_name, style=COLORS['tool_called']))


def print_interrupt(message: str):
    console.print(Panel(
        Text(message, style=COLORS['interrupt']),
        title=f"[{COLORS['interrupt_dark']}]⏸  APPROVAL NEEDED[/{COLORS['interrupt_dark']}]",
        border_style=COLORS['interrupt'],
        expand=False,
    ))


# --- Paths and directories ---
CURRENT_DIR = Path.cwd()
PROJECT_ROOT = CURRENT_DIR
SANDBOX_DIR = PROJECT_ROOT / "sandbox"
STORE_DIR = PROJECT_ROOT / "store"
CHECKPOINT_DB = STORE_DIR / "checkpoints.sqlite"

STORE_DIR.mkdir(parents=True, exist_ok=True)
SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
print(f"Store directory ready at: {STORE_DIR.resolve()}")
print(f"Sandbox ready at: {SANDBOX_DIR.resolve()}")
print(f"Checkpoint DB at: {CHECKPOINT_DB.resolve()}")


# ---------------------------------------------------------------------------
# Imports & globals — populated during async setup
# ---------------------------------------------------------------------------

mcp_tools: list = []
model = None
checkpointer = None
agent = None
mcp_client: MultiServerMCPClient | None = None

# --- Prompts and agent framework (sync, safe at module level) ---
from prompts.system_prompt import SYSTEM_PROMPT
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain_core.tools import StructuredTool
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

backend = FilesystemBackend(root_dir=str(SANDBOX_DIR), virtual_mode=True)


# ---------------------------------------------------------------------------
# Async setup helpers — called from setup_agent()
# ---------------------------------------------------------------------------


async def _setup_mcp_client():
    """Create the MCP client and store it globally."""
    global mcp_client
    mcp_client = MultiServerMCPClient({
        "n8n-mcp": {
            "transport": "stdio",
            "command": "npx",
            "args": ["n8n-mcp"],
            "env": {
                "MCP_MODE": "stdio",
                "LOG_LEVEL": "error",
                "DISABLE_CONSOLE_OUTPUT": "true",
                "N8N_API_URL": os.getenv("N8N_API_URL") or "http://localhost:5678",
                "N8N_API_KEY": os.getenv("N8N_API_KEY") or "",
            },
        }
    })


async def _setup_llm():
    """Instantiate the Ollama-backed LLM."""
    from langchain_ollama import ChatOllama

    api_key = os.getenv("OLLAMA_API_KEY")
    return ChatOllama(
        model="nemotron-3-ultra:cloud",
        base_url="https://ollama.com",
        client_kwargs={"headers": {"Authorization": f"Bearer {api_key}"}},
        temperature=0.2,
    )


async def _setup_checkpointer():
    """Open the SQLite connection for LangGraph checkpointing."""
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    conn = await aiosqlite.connect(str(CHECKPOINT_DB))
    return AsyncSqliteSaver(conn)


# ---------------------------------------------------------------------------
# Tool and schema definitions (module-level, stateless)
# ---------------------------------------------------------------------------


class WriteJsonFileSchema(BaseModel):
    file_path: str = Field(description="Path where the JSON file should be created, relative to the sandbox root.")
    content: dict | list = Field(description="The JSON-serializable object to write (dict or list).")


def write_json_file(file_path: str, content: dict | list) -> str:
    """Write a JSON-serializable object to a file in the sandbox. Overwrites if it already exists."""
    file_path = file_path.lstrip("/")
    if file_path == "sandbox" or file_path.startswith("sandbox/"):
        file_path = file_path[len("sandbox"):].lstrip("/")
    target = (SANDBOX_DIR / file_path).resolve()
    sandbox_root = SANDBOX_DIR.resolve()
    if sandbox_root != target and sandbox_root not in target.parents:
        raise RuntimeError(f"Path '{file_path}' escapes the sandbox directory.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(content, indent=2), encoding="utf-8")
    return f"Updated file {file_path}"


write_json_tool = StructuredTool.from_function(
    name="write_json_file",
    description="Write a JSON-serializable object (dict/list) to a file. Use this for n8n workflow JSON files.",
    func=write_json_file,
    args_schema=WriteJsonFileSchema,
)

MAX_VALIDATION_RETRIES = 3


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^`{3}(?:json)?\s*", "", text)
    text = re.sub(r"\s*`{3}+$", "", text)
    return text.strip()


def _extract_json_from_mcp_result(raw) -> dict:
    """Normalize MCP tool return values (str, dict, or list of content blocks) to a dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        return json.loads(raw)
    if isinstance(raw, list):
        for block in raw:
            if isinstance(block, dict) and block.get("type") == "text":
                return json.loads(block["text"])
            if isinstance(block, str):
                return json.loads(block)
        raise ValueError(f"No parseable text block found in MCP result: {raw!r}")
    raise TypeError(f"Unexpected MCP result type: {type(raw)}")


async def _call_validate(workflow_json: dict) -> dict:
    """Validate a workflow using the n8n-mcp validate_workflow tool."""
    validate_tool = next(
        (t for t in mcp_tools if t.name == "validate_workflow"),
        None,
    )
    if validate_tool is None:
        raise RuntimeError("validate_workflow tool not found in mcp_tools")
    raw = await validate_tool.ainvoke({"workflow": workflow_json})
    return _extract_json_from_mcp_result(raw)


async def _repair_workflow(workflow_json: dict, errors: list) -> dict:
    """Ask the model to fix ONLY the reported errors, returning corrected full JSON."""
    repair_prompt = (
        "You are repairing a broken n8n workflow JSON. Below is the current workflow "
        "and the validation errors it produced. Return ONLY the complete corrected "
        "JSON object — no markdown fences, no commentary, no explanation.\n\n"
        f"CURRENT WORKFLOW:\n{json.dumps(workflow_json, indent=2)}\n\n"
        f"VALIDATION ERRORS:\n{json.dumps(errors, indent=2)}"
    )
    response = await model.ainvoke([
        SystemMessage(content="You output only valid JSON. Never include markdown fences or prose."),
        HumanMessage(content=repair_prompt),
    ])
    cleaned = _strip_code_fences(response.content)
    return json.loads(cleaned)


class BuildValidateSchema(BaseModel):
    workflow_name: str = Field(description="Short kebab-case slug for this workflow, e.g. 'lead-triage-webhook-to-slack'.")
    workflow_json: dict = Field(description="The full assembled n8n workflow as a JSON-serializable dict.")


async def build_workflow_with_validation(workflow_name: str, workflow_json: dict) -> dict:
    """Validate a workflow JSON against n8n-mcp's validator, self-repairing on failure.

    Retries up to MAX_VALIDATION_RETRIES times. Returns a dict with status either
    'valid' or 'failed_after_retries' — never raises for ordinary validation failures.
    """
    current = workflow_json
    last_result = None
    for attempt in range(1, MAX_VALIDATION_RETRIES + 1):
        result = await _call_validate(current)
        last_result = result
        if result.get("valid"):
            return {
                "status": "valid",
                "attempts": attempt,
                "workflow_name": workflow_name,
                "workflow_json": current,
            }
        errors = result.get("errors", [])
        if attempt == MAX_VALIDATION_RETRIES:
            break
        try:
            current = await _repair_workflow(current, errors)
        except Exception as e:
            return {
                "status": "failed_after_retries",
                "attempts": attempt,
                "workflow_name": workflow_name,
                "errors": errors,
                "repair_error": str(e),
            }
    return {
        "status": "failed_after_retries",
        "attempts": MAX_VALIDATION_RETRIES,
        "workflow_name": workflow_name,
        "errors": last_result.get("errors", []) if last_result else [],
        "workflow_json": current,
    }


build_validate_tool = StructuredTool.from_function(
    name="build_workflow_with_validation",
    description=(
        "Validates an assembled n8n workflow JSON and self-repairs it automatically "
        "on failure, retrying internally. Call this ONCE per workflow instead of "
        "manually calling validate_workflow yourself."
    ),
    coroutine=build_workflow_with_validation,
    args_schema=BuildValidateSchema,
)


class ApprovalSchema(BaseModel):
    workflow_name: str = Field(description="Slug this workflow will be saved/deployed under.")
    nodes_added: list[str] = Field(description="Display names of every node in the workflow.")
    credentials_used: list[str] = Field(default_factory=list, description="Credential types referenced, e.g. 'slackApi'. Empty list if none.")
    external_services: list[str] = Field(description="Plain-language real-world services this workflow will actually touch when active.")
    summary: str = Field(description="1-3 plain-language sentences describing what this workflow does when it runs.")


def request_human_approval(
    workflow_name: str,
    nodes_added: list[str],
    credentials_used: list[str],
    external_services: list[str],
    summary: str,
) -> str:
    """Pause execution and ask the human to approve this workflow before it is written to disk."""
    decision = interrupt({
        "type": "approval_request",
        "workflow_name": workflow_name,
        "nodes_added": nodes_added,
        "credentials_used": credentials_used,
        "external_services": external_services,
        "summary": summary,
    })
    approved = decision.get("approved", False) if isinstance(decision, dict) else bool(decision)
    feedback = decision.get("feedback", "") if isinstance(decision, dict) else ""
    if approved:
        return "APPROVED by user. Proceed to write the workflow file with write_json_file."
    return f"REJECTED by user. Feedback: {feedback or '(none given)'}. Do not write any file — return to PLAN or BUILD to address this."


approval_tool = StructuredTool.from_function(
    name="request_human_approval",
    description="Pauses and requests explicit human approval, showing a summary of nodes/credentials/external services, before any file is written.",
    func=request_human_approval,
    args_schema=ApprovalSchema,
)


# ---------------------------------------------------------------------------
# Agent setup
# ---------------------------------------------------------------------------


async def setup_agent():
    """Initialize MCP tools, the LLM, the checkpointer, and the deep agent.

    Must be called once before any chat() / approve() calls.
    Populates the module-level mcp_tools, model, checkpointer, and agent globals.
    """
    global mcp_tools, model, checkpointer, agent

    await _setup_mcp_client()
    mcp_tools = await mcp_client.get_tools()
    print(f"Loaded {len(mcp_tools)} tools from n8n-mcp")

    model = await _setup_llm()
    checkpointer = await _setup_checkpointer()

    all_tools = mcp_tools + [write_json_tool, build_validate_tool, approval_tool]
    agent = create_deep_agent(
        model=model,
        tools=all_tools,
        system_prompt=SYSTEM_PROMPT,
        backend=backend,
        checkpointer=checkpointer,
    )


# ---------------------------------------------------------------------------
# Streaming chat helper
# ---------------------------------------------------------------------------


async def _stream_response(astream_generator):
    """Stream agent response using rich Live for smooth token-by-token display."""
    live_text = Text("", style=COLORS['response'])
    reasoning_text = Text("", style=COLORS['reasoning'])
    in_reasoning = False

    with Live(live_text, console=console, refresh_per_second=30, vertical_overflow="visible") as live:
        async for chunk, metadata in astream_generator:
            # Tool calls — show only the tool name, hide verbose args
            if isinstance(chunk, AIMessage) and chunk.tool_calls:
                for tc in chunk.tool_calls:
                    if in_reasoning and reasoning_text.plain:
                        console.print()
                        print_reasoning(reasoning_text.plain)
                        reasoning_text = Text("", style=COLORS['reasoning'])
                        in_reasoning = False
                    print_tool_called(tc['name'])

            # Content streaming — AIMessage only. ToolMessage also has `.content`,
            # but that's the raw MCP tool result (can be thousands of lines); we
            # never want to render that to the terminal.
            elif isinstance(chunk, AIMessage) and chunk.content:
                content = chunk.content

                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            # Reasoning/thinking blocks — print immediately in real-time
                            if block.get('type') == 'thinking' or block.get('type') == 'reasoning':
                                in_reasoning = True
                                thinking = block.get('thinking', block.get('reasoning', ''))
                                if thinking:
                                    reasoning_text.append(thinking)
                                    print_reasoning(thinking)
                            elif block.get('type') == 'text':
                                text = block.get('text', '')
                                if text:
                                    live_text.append(text)
                                    live.update(live_text)
                elif isinstance(content, str):
                    live_text.append(content)
                    live.update(live_text)

        # Flush any remaining reasoning
        if in_reasoning and reasoning_text.plain:
            console.print()
            print_reasoning(reasoning_text.plain)

    console.print()


async def _check_for_interrupt(thread_id: str) -> bool:
    """Check if the agent is paused at an approval interrupt and print a prompt."""
    config = {"configurable": {"thread_id": thread_id}}
    state = await agent.aget_state(config)
    for task in state.tasks:
        for intr in task.interrupts:
            if isinstance(intr.value, dict) and intr.value.get("type") == "approval_request":
                print_interrupt(intr.value.get("summary", "Workflow requires approval"))
                console.print(
                    f"\n[{COLORS['interrupt_dark']}]"
                    f'Call: approve(thread_id="{thread_id}", approved=True)  '
                    f"# or approved=False, feedback=\"...\""
                    f"[/{COLORS['interrupt_dark']}]"
                )
                return True
    return False


async def chat(user_input: str, thread_id: str = "session-1"):
    """Send a user message to the agent, stream the response, and handle approval interrupts interactively."""
    config = {"configurable": {"thread_id": thread_id}}

    print_separator()
    print_header("user", "user")
    console.print(user_input)

    print_separator()
    print_header("botchain-ai", "botchain")

    await _stream_response(agent.astream(
        {"messages": [HumanMessage(content=user_input)]},
        config=config,
        stream_mode="messages",
    ))

    print_separator()

    # If the agent paused for approval, prompt the user in-terminal
    while await _check_for_interrupt(thread_id):
        decision = input("approve> ").strip().lower()
        if decision == "approve":
            await approve(thread_id, True)
        else:
            await approve(thread_id, False, feedback=decision or "rejected by user")


async def approve(thread_id: str, approved: bool, feedback: str = ""):
    """Resume the agent after a human approval/rejection interrupt."""
    config = {"configurable": {"thread_id": thread_id}}
    action = "Approved" if approved else "Rejected"
    style = COLORS['reasoning'] if approved else COLORS['interrupt']
    console.print(f"\n[{style}]{action} — resuming...[/{style}]\n")

    await _stream_response(agent.astream(
        Command(resume={"approved": approved, "feedback": feedback}),
        config=config,
        stream_mode="messages",
    ))

    print_separator()


# ---------------------------------------------------------------------------
# Entry point — terminal chat loop
# ---------------------------------------------------------------------------


async def main():
    """Wire up the agent and run an interactive terminal chat loop."""
    await setup_agent()

    thread_id = "session-1"
    print("\nBotChain AI is ready. Type your automation request or 'quit' to exit.\n")

    while True:
        try:
            user_input = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break

        await chat(user_input, thread_id=thread_id)


if __name__ == "__main__":
    asyncio.run(main())
