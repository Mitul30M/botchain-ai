"""Tests for surviving n8n-mcp's validate_workflow schema violation.

n8n-mcp declares errors[].details as a string but sometimes returns a
structured {"fix": ...} object; langchain_mcp_adapters' strict schema check
then raises RuntimeError and discards the result. These tests pin the
behaviour that keeps the build alive: details get stringified, and the
validate step degrades to a recoverable failure instead of crashing.
"""

import json

import pytest

from app.services.agent import (
    _extract_validate_result,
    _stringify_error_fields,
    build_workflow_with_validation,
)

FIX_OBJECT = {
    "fix": (
        "Move these properties from node.parameters to the node level. "
        'Example:\n{"name": "Slack", "type": "n8n-nodes-base.slack"}'
    )
}

VENDOR_RESULT = {
    "valid": False,
    "errors": [
        {"code": "workflow_nodes_do_not_exist", "message": "Node does not exist", "details": FIX_OBJECT},
        {"code": "invalid_type", "message": "type is not a valid node type", "details": "nodes-base.slack"},
    ],
}


class _FakeModel:
    def __init__(self, repair_json: dict):
        self._repair = repair_json

    async def ainvoke(self, messages):
        reply = type("Reply", (), {"content": json.dumps(self._repair)})()
        return reply


class _RaisingValidateTool:
    async def ainvoke(self, args):
        raise RuntimeError(
            "Invalid structured content returned by tool validate_workflow: "
            f"{FIX_OBJECT!r} is not of type 'string'"
        )


def test_stringify_error_fields_coerces_object_details():
    errors = _stringify_error_fields(VENDOR_RESULT["errors"])
    assert isinstance(errors[0]["details"], str)
    assert "Move these properties" in errors[0]["details"]
    assert errors[1]["details"] == "nodes-base.slack"


def test_stringify_error_fields_handles_non_mapping_items():
    assert _stringify_error_fields(["plain", {"details": {"fix": "x"}}]) == [
        "plain",
        {"details": '{"fix": "x"}'},
    ]
    assert _stringify_error_fields("not a list") == []


def test_extract_validate_result_stringifies_nested_details():
    result = _extract_validate_result(VENDOR_RESULT)
    assert isinstance(result["errors"][0]["details"], str)
    assert result["errors"][1]["message"] == "type is not a valid node type"


def test_extract_validate_result_from_json_string():
    result = _extract_validate_result(json.dumps(VENDOR_RESULT))
    assert isinstance(result["errors"][0]["details"], str)


def test_extract_validate_result_accepts_plain_object():
    result = _extract_validate_result({"valid": True, "errors": [], "warnings": []})
    assert result["valid"] is True
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_build_workflow_survives_schema_violation():
    workflow = {"name": "wf", "nodes": [], "connections": {}}
    tool = _RaisingValidateTool()
    model = _FakeModel(workflow)

    result = await build_workflow_with_validation("wf", workflow, tool, model)

    assert result["status"] == "failed_after_retries"
    assert result["attempts"] == 3
    errs = result["errors"]
    assert len(errs) >= 1
    assert isinstance(errs[0]["details"], str)
    assert "Invalid structured content" in errs[0]["details"]