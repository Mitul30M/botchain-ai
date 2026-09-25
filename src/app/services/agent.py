from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from typing import Annotated, Any, TypedDict

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import StructuredTool
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field, create_model

from app.prompts import (
    REPAIR_SYSTEM_PROMPT,
    build_build_system_prompt,
    build_plan_system_prompt,
)

PLAN_SYSTEM_PROMPT = build_plan_system_prompt()
BUILD_SYSTEM_PROMPT = build_build_system_prompt()

MAX_VALIDATION_RETRIES = 3
MAX_GRAPH_RETRIES = 3
MAX_TOOL_LOOP_TURNS = 30

# Tools bound during build. Management tools (n8n_*) are deliberately excluded —
# they need a live n8n instance and are not part of the curated workflow surface.
BUILD_TOOL_NAMES = {
    "tools_documentation",
    "search_nodes",
    "get_node",
    "validate_node",
    "search_templates",
    "get_template",
    "validate_workflow",
}

# ---------------------------------------------------------------------------
# State & structured output
# ---------------------------------------------------------------------------


class PlanOutput(BaseModel):
    message: str = Field(description="Your plain-language reply to the user this turn.")
    ready_to_confirm: bool = Field(
        default=False,
        description="True only when the full spec is complete and open_questions is empty.",
    )
    goal: str | None = None
    trigger_type: str | None = None
    services_involved: list[str] = Field(default_factory=list)
    conditions_logic: str | None = None
    data_flow: str | None = None
    constraints: str | None = None
    open_questions: list[str] = Field(default_factory=list)


PLAN_SCHEMA = PlanOutput.model_json_schema()


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    phase: str
    spec: dict[str, Any]
    workflow_json: dict[str, Any] | None
    workflow_name: str | None
    validation_status: str | None
    validation_errors: list[Any]
    retry_count: int
    build_feedback: str | None
    confirm_summary: str | None


# ---------------------------------------------------------------------------
# Ported helpers
# ---------------------------------------------------------------------------


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^`{3}(?:json)?\s*", "", text)
    text = re.sub(r"\s*`{3}+$", "", text)
    return text.strip()


def _extract_text_payload(content: Any) -> str:
    """Reduce an AIMessage/chunk content (str or list of blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def _spec_summary(spec: Mapping[str, Any]) -> str:
    goal = spec.get("goal") or "an unspecified automation"
    trigger = spec.get("trigger_type") or "an unspecified trigger"
    services = spec.get("services_involved") or []
    data_flow = spec.get("data_flow") or ""
    conditions = spec.get("conditions_logic")
    lines = [f"- Goal: {goal}", f"- Trigger: {trigger}"]
    if data_flow:
        lines.append(f"- Steps: {data_flow}")
    if conditions:
        lines.append(f"- Conditions: {conditions}")
    if services:
        lines.append(f"- Services: {', '.join(services)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Runtime helpers (closed over tools, no module globals)
# ---------------------------------------------------------------------------


def _make_write_json_tool(sandbox_dir: str) -> StructuredTool:
    """Write a JSON dict into a per-build sandbox dir; returns a short confirmation."""

    def write_json_file(file_path: str, content: dict | list) -> str:
        safe_path = file_path.lstrip("/")
        target = os.path.realpath(os.path.join(sandbox_dir, safe_path))
        root = os.path.realpath(sandbox_dir)
        if root != target and root not in os.path.commonpath([root, target]):
            raise RuntimeError("write_json_file path escapes the sandbox directory.")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(content, fh, indent=2)
        return f"Updated file {safe_path}"

    Schema = create_model(
        "WriteJsonFile",
        file_path=(str, Field(description="Destination path, e.g. 'workflow.json'.")),
        content=(dict, Field(description="The workflow dict (dict or list).")),
    )
    return StructuredTool.from_function(
        name="write_json_file",
        description=(
            "Write the complete assembled n8n workflow as a JSON-serializable dict. "
            "Use this exactly once to submit the finished workflow."
        ),
        func=write_json_file,
        args_schema=Schema,
    )


class _NodeLookupCache:
    """In-memory cache for expensive, read-only search_nodes/get_node results."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def key(self, name: str, args: dict) -> str:
        return f"{name}:{json.dumps(args, sort_keys=True, default=str)}"

    def get(self, name: str, args: dict) -> Any | None:
        return self._data.get(self.key(name, args))

    def put(self, name: str, args: dict, result: Any) -> None:
        self._data[self.key(name, args)] = result


async def _call_cached(
    tool, name: str, args: dict, cache: _NodeLookupCache
) -> Any:
    if name in {"search_nodes", "get_node", "tools_documentation"}:
        cached = cache.get(name, args)
        if cached is not None:
            return cached
        result = await tool.ainvoke(args)
        cache.put(name, args, result)
        return result
    return await tool.ainvoke(args)


async def _repair_workflow(model, workflow_json: dict, errors: list) -> dict:
    prompt = (
        "You are repairing a broken n8n workflow JSON. Below is the current workflow "
        "and the validation errors it produced. Return ONLY the complete corrected "
        "JSON object — no markdown fences, no commentary, no explanation.\n\n"
        f"CURRENT WORKFLOW:\n{json.dumps(workflow_json, indent=2)}\n\n"
        f"VALIDATION ERRORS:\n{json.dumps(errors, indent=2)}"
    )
    response = await model.ainvoke(
        [
            SystemMessage(content=REPAIR_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]
    )
    cleaned = _strip_code_fences(_extract_text_payload(response.content))
    return json.loads(cleaned)


async def build_workflow_with_validation(
    workflow_name: str,
    workflow_json: dict,
    validate_tool,
    model,
) -> dict:
    """Validate a workflow JSON against n8n-mcp's validator, self-repairing on failure.

    Retries up to MAX_VALIDATION_RETRIES times. Returns a dict with status either
    'valid' or 'failed_after_retries' — never raises for ordinary validation failures.
    """
    current = workflow_json
    last_result: dict | None = None
    for attempt in range(1, MAX_VALIDATION_RETRIES + 1):
        raw = await validate_tool.ainvoke({"workflow": current})
        result = _extract_validate_result(raw)
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
            current = await _repair_workflow(model, current, errors)
        except Exception as e:  # noqa: BLE001 - surface as a failed run, not a crash
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


def _extract_validate_result(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(_strip_code_fences(raw))
        except json.JSONDecodeError:
            return {"valid": False, "errors": [raw]}
    if isinstance(raw, list):
        for block in raw:
            if isinstance(block, Mapping) and block.get("type") == "text":
                text = block.get("text", "")
                try:
                    return json.loads(_strip_code_fences(text))
                except json.JSONDecodeError:
                    return {"valid": False, "errors": [text]}
    return {"valid": False, "errors": [f"Unexpected validation result: {raw!r}"]}


# ---------------------------------------------------------------------------
# Node functions
# ---------------------------------------------------------------------------


def _plan_router(state: dict) -> str:
    return "confirm" if state.get("phase") == "confirm" else END


def _build_validate_router(state: dict) -> str:
    return "build" if state.get("phase") == "build" else END


async def _invoke_structured_with_retry(
    plan_model, model, messages: list[AnyMessage]
) -> PlanOutput:
    """Call the structured plan model, escalating to a plain-JSON fallback.

    Ollama cloud's function-calling mode is unreliable: it occasionally returns
    prose instead of a tool call, and with long histories can return nothing at
    all. Strategy: retry structured output once with an explicit JSON directive,
    then fall back to a plain completion parsed from the raw JSON text.
    """
    strict_prompt = (
        "Return ONLY a strict JSON object matching the requested schema. "
        "No introductory text, no markdown fences.\n\n"
        f"SCHEMA: {json.dumps(PLAN_SCHEMA)}\n\n"
    )
    for attempt in range(2):
        try:
            out = await plan_model.ainvoke(messages)
            if out is None or not getattr(out, "goal", None):
                raise ValueError("plan model returned empty output")
            return out
        except Exception:  # noqa: BLE001 - fall back to plain JSON on any structured failure
            if attempt == 0:
                messages = [SystemMessage(content=strict_prompt), *messages]
    response = await model.ainvoke(messages)
    text = _extract_text_payload(response.content)
    data = json.loads(_strip_code_fences(text))
    return PlanOutput.model_validate(data)


def _make_plan_node(plan_model, model):
    async def plan_node(state: dict) -> dict:
        writer = get_stream_writer()
        writer({"status": "Planning…"})
        spec = state.get("spec") or {}
        messages: list[AnyMessage] = [SystemMessage(content=PLAN_SYSTEM_PROMPT)]
        messages.extend(state.get("messages", []))
        spec_context = (
            "CURRENT REQUIREMENTS SPEC (fill gaps; keep fields you have):\n"
            + json.dumps(spec, indent=2)
        )
        messages.append(SystemMessage(content=spec_context))

        out = await _invoke_structured_with_retry(plan_model, model, messages)
        new_spec = {
            "goal": out.goal,
            "trigger_type": out.trigger_type,
            "services_involved": list(out.services_involved or []),
            "conditions_logic": out.conditions_logic,
            "data_flow": out.data_flow,
            "constraints": out.constraints,
            "open_questions": list(out.open_questions or []),
        }
        update: dict[str, Any] = {
            "phase": "confirm" if out.ready_to_confirm else "plan",
            "spec": new_spec,
            "messages": [AIMessage(content=out.message)],
        }
        if out.ready_to_confirm:
            update["confirm_summary"] = out.message
        return update

    return plan_node


def _make_confirm_node() -> Any:
    def confirm_node(state: dict) -> Command:
        decision = interrupt(
            {
                "type": "approval_request",
                "summary": state.get("confirm_summary")
                or _spec_summary(state.get("spec") or {}),
            }
        )
        if isinstance(decision, Mapping) and decision.get("approved"):
            return Command(
                update={
                    "phase": "build",
                    "messages": [AIMessage(content="Got it — building your workflow now.")],
                },
                goto="build",
            )
        feedback = decision.get("feedback", "") if isinstance(decision, Mapping) else ""
        return Command(
            update={
                "phase": "plan",
                "messages": [
                    HumanMessage(
                        content=f"Changes requested by user: {feedback or '(no detail given)'} "
                        "Update the spec accordingly."
                    )
                ],
            },
            goto="plan",
        )

    return confirm_node


def _make_build_node(model, tools_by_name: dict, cache: _NodeLookupCache):
    build_tools = [tools_by_name[n] for n in sorted(BUILD_TOOL_NAMES) if n in tools_by_name]

    async def build_node(state: dict) -> dict:
        writer = get_stream_writer()
        sandbox_dir = tempfile.mkdtemp(prefix="botchain-build-")
        write_tool = _make_write_json_tool(sandbox_dir)
        bound = model.bind_tools([*build_tools, write_tool])

        loop_messages: list[AnyMessage] = [SystemMessage(content=BUILD_SYSTEM_PROMPT)]
        if feedback := state.get("build_feedback"):
            loop_messages.append(HumanMessage(content=feedback))
        loop_messages.extend(state.get("messages", []))

        captured_workflow: dict | None = None
        captured_name = "workflow.json"

        for _ in range(MAX_TOOL_LOOP_TURNS):
            response = await bound.ainvoke(loop_messages)
            if not response.tool_calls:
                break
            loop_messages.append(response)
            for tc in response.tool_calls:
                name, args = tc["name"], tc["args"]
                if name == "write_json_file":
                    writer({"status": "Assembling the workflow file…"})
                    content = args.get("content")
                    if isinstance(content, dict):
                        captured_workflow = content
                        captured_name = str(args.get("file_path") or captured_name)
                    result = await write_tool.ainvoke(args)
                elif name in tools_by_name:
                    writer({"status": "Looking that node up…"})
                    result = await _call_cached(
                        tools_by_name[name], name, args, cache
                    )
                else:
                    writer({"status": "Working…"})
                    result = f"Unknown tool: {name}"
                loop_messages.append(
                    ToolMessage(content=str(result), tool_call_id=tc["id"])
                )

        # Fallback: model answered without calling write_json_file — try to parse JSON.
        if captured_workflow is None and loop_messages:
            last = loop_messages[-1]
            if isinstance(last, AIMessage):
                text = _extract_text_payload(last.content)
                cleaned = _strip_code_fences(text)
                try:
                    parsed = json.loads(cleaned)
                    if isinstance(parsed, dict):
                        captured_workflow = parsed
                except json.JSONDecodeError:
                    pass

        if captured_workflow is None:
            return {
                "phase": "done",
                "validation_status": "build_failed",
                "validation_errors": ["No workflow JSON was produced by the build step."],
                "messages": [
                    AIMessage(
                        content=(
                            "I couldn't assemble a workflow from the confirmed plan. "
                            "Something went wrong during the build step — please try again, "
                            "and let me know if you saw an error."
                        )
                    )
                ],
            }

        return {
            "phase": "build",
            "workflow_json": captured_workflow,
            "workflow_name": captured_name,
        }

    return build_node


def _make_validate_node(model, tools_by_name: dict):
    validate_tool = tools_by_name.get("validate_workflow")

    async def validate_node(state: dict) -> dict:
        writer = get_stream_writer()
        writer({"status": "Checking the workflow…"})
        workflow = state.get("workflow_json")
        name = state.get("workflow_name") or "workflow.json"
        if workflow is None:
            return {
                "phase": "done",
                "validation_status": "build_failed",
                "validation_errors": ["No workflow JSON to validate."],
                "messages": [AIMessage(content="No workflow was produced to validate.")],
            }
        if validate_tool is None:
            return {
                "phase": "done",
                "validation_status": "build_failed",
                "validation_errors": ["validate_workflow tool unavailable."],
                "messages": [AIMessage(content="Validation tooling is unavailable — please retry shortly.")],
            }

        result = await build_workflow_with_validation(
            name, workflow, validate_tool, model
        )
        retries = state.get("retry_count", 0)

        if result["status"] == "valid":
            writer({"status": "Workflow validated. Done."})
            return {
                "phase": "done",
                "validation_status": "valid",
                "validation_errors": [],
                "retry_count": retries,
                "workflow_json": result.get("workflow_json") or workflow,
                "messages": [
                    AIMessage(
                        content=(
                            f"Your workflow is ready to import. It passed n8n validation "
                            f"after {result.get('attempts', 1)} attempt(s).\n\nImport it in "
                            f"n8n via Workflows \u2192 Import from File."
                        )
                    )
                ],
            }

        errors = result.get("errors", [])
        retries += 1
        if retries <= MAX_GRAPH_RETRIES:
            writer({"status": "Fixing validation errors…"})
            feedback = (
                "The workflow JSON you submitted failed n8n validation. Here are the "
                "specific errors and the failed workflow. Return ONLY a corrected complete "
                "workflow JSON dict via write_json_file.\n\n"
                f"VALIDATION ERRORS:\n{json.dumps(errors, indent=2)}\n\n"
                f"WORKFLOW:\n{json.dumps(workflow, indent=2)}"
            )
            return {
                "phase": "build",
                "retry_count": retries,
                "validation_status": "needs_repair",
                "validation_errors": errors,
                "build_feedback": feedback,
            }

        return {
            "phase": "done",
            "validation_status": "failed_after_retries",
            "validation_errors": errors,
            "retry_count": retries,
            "workflow_json": result.get("workflow_json") or workflow,
            "messages": [
                AIMessage(
                    content=(
                        "I wasn't able to get the workflow to pass n8n's validator after "
                        "several attempts. Here's what's still wrong:\n\n"
                        + _render_errors(errors)
                        + "\n\nIf you can tell me more about any of those items — real field "
                        "names from the sources involved, exact service endpoints, or an "
                        "example of the data the workflow handles — I can fix it."
                    )
                )
            ],
        }

    return validate_node


def _render_errors(errors: list[Any]) -> str:
    lines: list[str] = []
    for err in errors[:10]:
        if isinstance(err, Mapping):
            lines.append(f"- {err.get('message') or err.get('error') or err}")
        else:
            lines.append(f"- {err}")
    if len(errors) > 10:
        lines.append(f"- … and {len(errors) - 10} more.")
    return "\n".join(lines) or "- (no error details returned)"


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------


def create_agent(model, mcp_tools: list, checkpointer) -> object:
    """Compile the Plan → Confirm → Build → Validate StateGraph.

    The compiled graph is the object stored on ``app.state.agent``; routes call
    ``agent.astream(...)`` with ``stream_mode=["messages", "custom"]`` and resume
    interrupted runs via ``agent.astream(Command(resume=...), ...)``.
    """
    tools_by_name = {t.name: t for t in mcp_tools}
    cache = _NodeLookupCache()

    plan_model = model.with_structured_output(PlanOutput, method="function_calling")

    graph = StateGraph(AgentState)
    graph.add_node("plan", _make_plan_node(plan_model, model))
    graph.add_node("confirm", _make_confirm_node())
    graph.add_node("build", _make_build_node(model, tools_by_name, cache))
    graph.add_node("validate", _make_validate_node(model, tools_by_name))

    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", _plan_router, {"confirm": "confirm", END: END})
    graph.add_edge("build", "validate")
    graph.add_conditional_edges(
        "validate", _build_validate_router, {"build": "build", END: END}
    )

    return graph.compile(checkpointer=checkpointer)