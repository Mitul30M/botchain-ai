"""Unit tests for the structured-output fallback in services/agent.py."""

import json

import pytest
from langchain_core.messages import AIMessage

from app.services.agent import (
    PLAN_SCHEMA,
    PlanOutput,
    _invoke_structured_with_retry,
)


class _FakePlanModel:
    """Structured model that never produces output — forces the fallback."""

    def __init__(self, failures=2):
        self.failures = failures

    async def ainvoke(self, messages):
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("forced structured failure")


class _FakeModel:
    def __init__(self, reply):
        self._reply = reply

    async def ainvoke(self, messages):
        return AIMessage(content=self._reply)


async def test_fallback_parses_plain_json_reply():
    payload = {
        "message": "Spec looks good.",
        "ready_to_confirm": True,
        "goal": "Post contact form submissions to Slack.",
        "trigger_type": "Webhook",
        "services_involved": ["Slack"],
    }
    out = await _invoke_structured_with_retry(
        _FakePlanModel(), _FakeModel(json.dumps(payload)), []
    )
    assert isinstance(out, PlanOutput)
    assert out.goal == "Post contact form submissions to Slack."
    assert out.ready_to_confirm is True
    assert out.services_involved == ["Slack"]


async def test_fallback_handles_code_fences():
    payload = {
        "message": "Working.",
        "ready_to_confirm": False,
        "goal": "Migrate Zendesk tickets.",
    }
    reply = f"```json\n{json.dumps(payload)}\n```"
    out = await _invoke_structured_with_retry(_FakePlanModel(), _FakeModel(reply), [])
    assert out.goal == "Migrate Zendesk tickets."


async def test_structured_success_short_circuits_fallback():
    class _GoodPlanModel:
        async def ainvoke(self, messages):
            return PlanOutput(message="ok", goal="g")

    out = await _invoke_structured_with_retry(_GoodPlanModel(), _FakeModel("ignored"), [])
    assert out.goal == "g"


async def test_fallback_includes_schema_in_prompt():
    captured = []

    class _CaptureModel:
        async def ainvoke(self, messages):
            captured.extend(m.content for m in messages)
            return AIMessage(
                content=json.dumps({"message": "m", "ready_to_confirm": False, "goal": "g"})
            )

    out = await _invoke_structured_with_retry(_FakePlanModel(), _CaptureModel(), [])
    assert out.goal == "g"
    assert any(PLAN_SCHEMA["title"] in c for c in captured)


async def test_fallback_raises_on_invalid_json():
    class _EarlyBoomPlanModel:
        async def ainvoke(self, messages):
            raise RuntimeError("boom")

    with pytest.raises(ValueError):
        await _invoke_structured_with_retry(
            _EarlyBoomPlanModel(), _FakeModel("not json"), []
        )