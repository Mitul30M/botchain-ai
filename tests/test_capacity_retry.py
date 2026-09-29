"""Mistral capacity-429 resilience.

Pins the fix for the ``backend_out_of_capacity`` (HTTP 429, code 3505) crash:
langchain-mistralai's retry decorator excludes ``HTTPStatusError``, so without
an app-level wrapper a capacity blip aborts the build/plan stream. These tests
cover the retry helper itself plus the graceful-degradation paths in the build
and planner nodes.
"""

from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage

import app.services.agent as agent_mod
import app.services.llm as llm_mod
from app.services.agent import (
    _PLAN_DEGRADED_MESSAGE,
    _invoke_structured_with_retry,
    _make_build_node,
)
from app.services.llm import MistralCapacityError, ainvoke_with_capacity_retries


def _capacity_429() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.mistral.ai/v1/chat/completions")
    response = httpx.Response(
        429,
        request=request,
        json={"type": "backend_out_of_capacity", "code": "3505", "raw_status_code": 429},
    )
    return httpx.HTTPStatusError("429 capacity", request=request, response=response)


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.mistral.ai/v1/chat/completions")
    response = httpx.Response(status, request=request, json={"error": "nope"})
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(llm_mod, "asyncio", SimpleNamespace(sleep=_no_sleep))


class _SeqCallable:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def test_retries_capacity_429_then_succeeds():
    call = _SeqCallable([_capacity_429(), _capacity_429(), AIMessage(content="ok")])
    result = await ainvoke_with_capacity_retries(call, [], retries=3)
    assert result.content == "ok"
    assert call.calls == 3


async def test_non_429_raises_without_retry():
    call = _SeqCallable([_status_error(403)])
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await ainvoke_with_capacity_retries(call, [], retries=3)
    assert exc.value.response.status_code == 403
    assert call.calls == 1


async def test_exhausted_retries_raise_capacity_error():
    call = _SeqCallable([_capacity_429()] * 4)
    with pytest.raises(MistralCapacityError):
        await ainvoke_with_capacity_retries(call, [], retries=3)
    assert call.calls == 4


async def test_zero_retries_is_single_attempt():
    call = _SeqCallable([_capacity_429()])
    with pytest.raises(MistralCapacityError):
        await ainvoke_with_capacity_retries(call, [], retries=0)
    assert call.calls == 1


async def test_default_retries_read_from_settings(monkeypatch):
    monkeypatch.setattr(
        llm_mod, "get_settings", lambda: SimpleNamespace(mistral_capacity_retries=2)
    )
    call = _SeqCallable([_capacity_429()] * 3)
    with pytest.raises(MistralCapacityError):
        await ainvoke_with_capacity_retries(call, [], retries=None)
    assert call.calls == 3


async def test_planner_degrades_gracefully_on_capacity():
    plan_model = _SeqCallable([_capacity_429()] * 5)
    model = _SeqCallable([_capacity_429()] * 5)
    out = await _invoke_structured_with_retry(plan_model, model, [])
    assert out.ready_to_confirm is False
    assert out.message == _PLAN_DEGRADED_MESSAGE


class _CapacityBuildModel:
    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        raise _capacity_429()


def _noop_writer():
    def writer(payload):
        return None

    return writer


async def test_build_node_degrades_gracefully_on_capacity(monkeypatch):
    monkeypatch.setattr(agent_mod, "get_stream_writer", _noop_writer)
    node = _make_build_node(_CapacityBuildModel(), {}, None)
    result = await node({"messages": [HumanMessage(content="build it")]})
    assert result["phase"] == "done"
    assert result["validation_status"] == "build_failed"
    assert result["messages"][0].content
