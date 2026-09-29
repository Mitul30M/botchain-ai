from __future__ import annotations

import json
import logging
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
from langchain_mcp_adapters.sessions import create_session
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.types import Command, interrupt
from mcp import types as mcp_types
from pydantic import BaseModel, Field, ValidationError, create_model

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

_PLAN_FALLBACK_RETRIES = 2
_PLAN_DEGRADED_MESSAGE = (
    "I had trouble turning that into a clean requirements plan. "
    "Could you restate what you'd like to automate, in a sentence or two?"
)

logger = logging.getLogger(__name__)

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


def _ensure_user_last(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Coerce a message history to end on a user (or tool) role.

    Some providers (Mistral) reject a request whose last message is an
    assistant or system turn — e.g. after an approval resumption the checked-in
    history ends on the confirm node's assistant "Got it — building…". Drop
    trailing assistant/system messages so the provider is happy; those turns
    carry no needed context for the model. Never empty the history entirely.
    """
    msgs = list(messages)
    while msgs and isinstance(msgs[-1], (AIMessage, SystemMessage)):
        msgs.pop()
    if not msgs:
        msgs.append(HumanMessage(content="Continue."))
    return msgs


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

    def write_json_file(file_path: str, content: Any) -> str:
        safe_path = file_path.lstrip("/")
        target = os.path.realpath(os.path.join(sandbox_dir, safe_path))
        root = os.path.realpath(sandbox_dir)
        if root != target and root not in os.path.commonpath([root, target]):
            raise RuntimeError("write_json_file path escapes the sandbox directory.")
        obj = _coerce_json_content(content)
        if obj is None:
            raise RuntimeError("write_json_file content must be a JSON object.")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2)
        return f"Updated file {safe_path}"

    Schema = create_model(
        "WriteJsonFile",
        file_path=(str, Field(description="Destination path, e.g. 'workflow.json'.")),
        content=(Any, Field(description="The workflow dict (dict, list, or JSON string).")),
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


def _coerce_json_content(value: Any) -> Any | None:
    """Return JSON-able content (dict/list) or a JSON string parsed to one; else None."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    if isinstance(value, (dict, list)):
        return value
    return None


def _extract_json_object(text: str) -> dict | None:
    """Parse a JSON object (dict) out of model text, leniently.

    Tries the whole (fence-stripped) text first, then the outermost ``{...}``
    span — models frequently wrap the workflow in prose or a fenced block.
    """
    cleaned = _strip_code_fences(text).strip()
    if not cleaned:
        return None
    candidates = [cleaned]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        candidates.append(cleaned[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _normalize_node_positions(node: dict) -> None:
    """Convert n8n node position shapes to canonical [x, y] array (in place)."""
    position = node.get("position")
    if isinstance(position, Mapping):
        x, y = position.get("x"), position.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            node["position"] = [x, y]


def _normalize_workflow_json(workflow: dict) -> dict:
    """Coerce common non-canonical model output into n8n's import schema.

    Smaller models frequently emit ``nodes`` as an object keyed by id,
    ``connections`` keyed by id with targets by id, ``main`` as a flat list
    instead of array-of-arrays, and ``position`` as ``{"x","y"}`` objects.
    This deterministically rewrites those shapes; already-canonical workflow
    JSON (e.g. the user-facing shape the plan produces) passes through with
    no changes. Returns a deep copy.
    """
    wf = json.loads(json.dumps(workflow))
    id_to_name: dict[str, str] = {}

    nodes = wf.get("nodes")
    if isinstance(nodes, dict):
        node_list: list[dict] = []
        for key, node in nodes.items():
            if not isinstance(node, dict):
                continue
            node = dict(node)
            name = str(node.get("name") or key)
            node["name"] = name
            _normalize_node_positions(node)
            id_to_name[str(key)] = name
            if isinstance(node.get("id"), str):
                id_to_name[node["id"]] = name
            node_list.append(node)
        wf["nodes"] = node_list
    elif isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, dict):
                _normalize_node_positions(node)
                if isinstance(node.get("id"), str) and isinstance(node.get("name"), str):
                    id_to_name[node["id"]] = node["name"]

    connections = wf.get("connections")
    if isinstance(connections, dict):
        new_connections: dict[str, Any] = {}
        for source_key, conn in connections.items():
            source_name = id_to_name.get(str(source_key), str(source_key))
            if isinstance(conn, dict):
                main = conn.get("main")
                if isinstance(main, list):
                    new_main: list[Any] = []
                    for entry in main:
                        if isinstance(entry, dict):
                            entry = dict(entry)
                            target = entry.get("node")
                            if target is not None and str(target) in id_to_name:
                                entry["node"] = id_to_name[str(target)]
                            new_main.append([entry])
                        elif isinstance(entry, list):
                            rewired = []
                            for sub in entry:
                                if isinstance(sub, dict):
                                    sub = dict(sub)
                                    if sub.get("node") is not None and str(sub["node"]) in id_to_name:
                                        sub["node"] = id_to_name[str(sub["node"])]
                                rewired.append(sub)
                            new_main.append(rewired)
                        else:
                            new_main.append(entry)
                    new_connections[source_name] = {**conn, "main": new_main}
                else:
                    new_connections[source_name] = conn
            else:
                new_connections[source_name] = conn
        wf["connections"] = new_connections

    return wf


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
    validate_connection: dict | None = None,
) -> dict:
    """Validate a workflow JSON against n8n-mcp's validator, self-repairing on failure.

    Retries up to MAX_VALIDATION_RETRIES times. Returns a dict with status either
    'valid' or 'failed_after_retries' — never raises for ordinary validation failures.
    """
    current = workflow_json
    last_result: dict | None = None
    for attempt in range(1, MAX_VALIDATION_RETRIES + 1):
        # Coerce non-canonical model output (nodes-by-id, flat main, etc.) back
        # to n8n's import schema before every check — including after repairs.
        current = _normalize_workflow_json(current)
        raw = await _invoke_validate_tool(validate_tool, current, validate_connection)
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


def _stringify_error_fields(errors: Any) -> list:
    """Coerce non-string ``details``/``message`` fields in validator errors.

    n8n-mcp sometimes returns ``errors[].details`` as a structured ``{"fix": ...}``
    object even though its declared output schema says string; the MCP adapter's
    strict schema check then raises and discards the whole result. Keeping those
    fields as strings is what downstream consumers (repair prompt, persisted
    meta, UI rendering) expect.
    """
    if not isinstance(errors, list):
        return []
    normalized: list[Any] = []
    for err in errors:
        if not isinstance(err, Mapping):
            normalized.append(err)
            continue
        item = dict(err)
        for field in ("details", "message"):
            value = item.get(field)
            if value is not None and not isinstance(value, str):
                item[field] = json.dumps(value, ensure_ascii=False)
        normalized.append(item)
    return normalized


def _extract_validate_result(raw: Any) -> dict:
    if isinstance(raw, dict):
        result = raw
    elif isinstance(raw, str):
        try:
            parsed = json.loads(_strip_code_fences(raw))
            result = parsed if isinstance(parsed, dict) else {"valid": False, "errors": [raw]}
        except json.JSONDecodeError:
            result = {"valid": False, "errors": [raw]}
    elif isinstance(raw, list):
        result = None
        for block in raw:
            if isinstance(block, Mapping) and block.get("type") == "text":
                text = block.get("text", "")
                try:
                    parsed = json.loads(_strip_code_fences(text))
                except json.JSONDecodeError:
                    result = {"valid": False, "errors": [text]}
                    break
                if isinstance(parsed, dict):
                    result = parsed
                    break
        if result is None:
            result = {"valid": False, "errors": [f"Unexpected validation result: {raw!r}"]}
    else:
        result = {"valid": False, "errors": [f"Unexpected validation result: {raw!r}"]}
    if isinstance(result.get("errors"), list):
        result["errors"] = _stringify_error_fields(result["errors"])
    return result


async def _raw_validate_call(connection: dict, workflow: dict) -> dict:
    """Run n8n-mcp's validate_workflow over a raw session, skipping schema checks.

    ``langchain_mcp_adapters`` raises ``RuntimeError`` when a tool's structured
    content violates its declared schema — which n8n-mcp does for
    ``errors[].details`` — and throws the whole result away. A raw
    ``send_request`` returns the structured content untouched, so all validation
    errors survive for the repair loop.
    """
    request = mcp_types.ClientRequest(
        mcp_types.CallToolRequest(
            params=mcp_types.CallToolRequestParams(
                name="validate_workflow", arguments={"workflow": workflow}
            )
        )
    )
    async with create_session(connection) as session:
        await session.initialize()
        result = await session.send_request(request, mcp_types.CallToolResult)
    if isinstance(result.structuredContent, Mapping):
        return dict(result.structuredContent)
    return _extract_validate_result(result.content)


async def _invoke_validate_tool(
    validate_tool, workflow: dict, validate_connection: dict | None
) -> Any:
    """Run validate_workflow, preferring the non-strict raw call when a
    connection is configured; otherwise degrade gracefully instead of crashing
    if the adapter rejects the tool's output."""
    if validate_connection is not None:
        try:
            return await _raw_validate_call(validate_connection, workflow)
        except Exception as e:  # noqa: BLE001 - surface as a failed run, not a crash
            logger.warning("raw validate_workflow call failed: %s", e)
            return {"valid": False, "errors": [{"message": str(e), "details": str(e)}]}
    try:
        return await validate_tool.ainvoke({"workflow": workflow})
    except RuntimeError as e:
        if "structured content" not in str(e):
            raise
        logger.warning("validate_workflow returned schema-violating content: %s", e)
        return {"valid": False, "errors": [{"message": str(e), "details": str(e)}]}


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
    then fall back to a plain completion parsed from the raw JSON text — that
    path is itself retried a couple of times, since empty replies are transient.
    If the model still returns nothing parseable, degrade to a friendly
    re-prompt instead of crashing the stream mid-run.
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
    for _ in range(_PLAN_FALLBACK_RETRIES):
        response = await model.ainvoke(messages)
        cleaned = _strip_code_fences(_extract_text_payload(response.content))
        if not cleaned:
            continue
        try:
            return PlanOutput.model_validate(json.loads(cleaned))
        except (ValueError, ValidationError):
            continue
    return PlanOutput(
        message=_PLAN_DEGRADED_MESSAGE,
        ready_to_confirm=False,
    )


def _make_plan_node(plan_model, model):
    async def plan_node(state: dict) -> dict:
        writer = get_stream_writer()
        writer({"status": "Planning…"})
        spec = state.get("spec") or {}
        messages: list[AnyMessage] = [
            SystemMessage(content=PLAN_SYSTEM_PROMPT),
            # Spec context sits beside the system prompt (not trailing the
            # history) — some providers reject a trailing system message.
            SystemMessage(
                content=(
                    "CURRENT REQUIREMENTS SPEC (fill gaps; keep fields you have):\n"
                    + json.dumps(spec, indent=2)
                )
            ),
        ]
        messages.extend(state.get("messages", []))

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
        # The sandbox is per-build scratch only (Railway's disk is ephemeral);
        # the finished workflow is returned in state and later persisted to
        # Message.meta, so the dir is torn down as soon as the loop ends.
        with tempfile.TemporaryDirectory(prefix="botchain-build-") as sandbox_dir:
            return await _run_build_loop(
                model,
                state,
                writer,
                sandbox_dir,
                build_tools,
                cache,
            )

    async def _run_build_loop(model, state: dict, writer, sandbox_dir: str, build_tools: list, cache) -> dict:
        write_tool = _make_write_json_tool(sandbox_dir)
        bound = model.bind_tools([*build_tools, write_tool])

        loop_messages: list[AnyMessage] = [SystemMessage(content=BUILD_SYSTEM_PROMPT)]
        if feedback := state.get("build_feedback"):
            loop_messages.append(HumanMessage(content=feedback))
        loop_messages.extend(state.get("messages", []))
        # After an approval resumption the history ends on an assistant turn
        # (e.g. "Got it — building…"); Mistral rejects a last-role assistant.
        loop_messages = _ensure_user_last(loop_messages)

        captured_workflow: dict | None = None
        captured_name = "workflow.json"

        for _ in range(MAX_TOOL_LOOP_TURNS):
            response = await bound.ainvoke(loop_messages)
            if not response.tool_calls:
                # Model finished in prose without a write_json_file call — keep
                # the reply so the fallback parser can harvest a workflow JSON
                # embedded in the text.
                loop_messages.append(response)
                break
            loop_messages.append(response)
            for tc in response.tool_calls:
                name, args = tc["name"], tc["args"]
                if name == "write_json_file":
                    writer({"status": "Assembling the workflow file…"})
                    content = _coerce_json_content(args.get("content"))
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

        # Fallback: no write_json_file call — scan backwards for an assistant
        # reply that embeds the workflow JSON in prose or a fenced block.
        if captured_workflow is None:
            for msg in reversed(loop_messages):
                if isinstance(msg, AIMessage):
                    workflow = _extract_json_object(
                        _extract_text_payload(msg.content)
                    )
                    if workflow is not None:
                        captured_workflow = workflow
                        break

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


def _make_validate_node(model, tools_by_name: dict, validate_connection: dict | None = None):
    validate_tool = tools_by_name.get("validate_workflow")

    async def validate_node(state: dict) -> dict:
        writer = get_stream_writer()
        writer({"status": "Checking the workflow…"})
        workflow = state.get("workflow_json")
        name = state.get("workflow_name") or "workflow.json"
        if isinstance(workflow, dict):
            workflow = _normalize_workflow_json(workflow)
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
            name, workflow, validate_tool, model, validate_connection=validate_connection
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


def create_agent(model, mcp_tools: list, checkpointer, validate_connection: dict | None = None) -> object:
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
    graph.add_node("validate", _make_validate_node(model, tools_by_name, validate_connection))

    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", _plan_router, {"confirm": "confirm", END: END})
    graph.add_edge("build", "validate")
    graph.add_conditional_edges(
        "validate", _build_validate_router, {"build": "build", END: END}
    )

    return graph.compile(checkpointer=checkpointer)