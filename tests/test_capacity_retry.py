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
from langgraph.graph import END

import app.services.agent as agent_mod
import app.services.llm as llm_mod
from app.services.agent import (
    _PLAN_DEGRADED_MESSAGE,
    _build_to_validate_router,
    _invoke_structured_with_retry,
    _make_build_node,
)
from app.services.llm import (
    MistralCapacityError,
    ModelTransientError,
    ModelTransportError,
    ainvoke_with_capacity_retries,
)


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


def _read_timeout() -> httpx.ReadTimeout:
    request = httpx.Request("POST", "https://api.mistral.ai/v1/chat/completions")
    return httpx.ReadTimeout("timed out reading response", request=request)


async def test_retries_read_timeout_then_succeeds():
    call = _SeqCallable([_read_timeout(), _read_timeout(), AIMessage(content="ok")])
    result = await ainvoke_with_capacity_retries(call, [], retries=3)
    assert result.content == "ok"
    assert call.calls == 3


async def test_exhausted_read_timeouts_raise_transport_error():
    call = _SeqCallable([_read_timeout()] * 3)
    with pytest.raises(ModelTransportError) as exc:
        await ainvoke_with_capacity_retries(call, [], retries=2)
    assert call.calls == 3
    assert "ReadTimeout" in str(exc.value)


async def test_read_timeout_is_a_transient_model_error():
    call = _SeqCallable([_read_timeout()])
    with pytest.raises(ModelTransientError):
        await ainvoke_with_capacity_retries(call, [], retries=0)


class _ReadTimeoutBuildModel:
    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        raise _read_timeout()


async def test_build_node_degrades_gracefully_on_read_timeout(monkeypatch):
    """A provider read timeout must not abort the build — degrade like capacity."""
    monkeypatch.setattr(agent_mod, "get_stream_writer", _noop_writer)
    node = _make_build_node(_ReadTimeoutBuildModel(), {}, None)
    result = await node({"messages": [HumanMessage(content="build it")]})
    assert result["phase"] == "done"
    assert result["validation_status"] == "build_failed"
    assert "temporarily unavailable" in result["validation_errors"][0]


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


def test_terminal_build_skips_validate_node():
    assert _build_to_validate_router({"phase": "build"}) == "validate"
    assert _build_to_validate_router({"phase": "done"}) == END


# ---------------------------------------------------------------------------
# Build-failure diagnostics: a build that captures no workflow used to return a
# generic apology with nothing logged, which made it the hardest failure to debug.
# ---------------------------------------------------------------------------


class _ScriptedBuildModel:
    """Replays a fixed list of responses, then prose, as a build model."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.bind_calls = 0

    def bind_tools(self, tools):
        self.bind_calls += 1
        return self

    async def ainvoke(self, messages):
        if self.responses:
            return self.responses.pop(0)
        return AIMessage(content="I'm done, here's the plan in prose.")


def _tool_call(name, args, call_id="c1"):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


async def _run_build(model, caplog):
    monkey = agent_mod.get_stream_writer
    agent_mod.get_stream_writer = _noop_writer
    try:
        node = _make_build_node(model, {}, None)
        return await node({"messages": [HumanMessage(content="build it")]})
    finally:
        agent_mod.get_stream_writer = monkey


async def test_build_failure_names_unparseable_write_payload(caplog):
    model = _ScriptedBuildModel(
        [_tool_call("write_json_file", {"content": "{not json at all", "file_path": "w.json"})]
    )
    with caplog.at_level("WARNING"):
        result = await _run_build(model, caplog)
    assert result["validation_status"] == "build_failed"
    assert "weren't valid JSON" in result["validation_errors"][0]
    assert "{not json at all" in result["validation_errors"][0]
    assert "unparseable_write_previews" in caplog.text


async def test_build_failure_names_prose_with_no_json(caplog):
    model = _ScriptedBuildModel([AIMessage(content="All done! Hope that helps.")])
    with caplog.at_level("WARNING"):
        result = await _run_build(model, caplog)
    assert "replied in prose" in result["validation_errors"][0]
    assert "Hope that helps" in result["validation_errors"][0]
    assert "build step produced no workflow" in caplog.text


async def test_build_failure_logs_unexpected_exception(caplog):
    class _Boom:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            raise RuntimeError("kaboom from the provider")

    with caplog.at_level("ERROR"), pytest.raises(RuntimeError):
        await _run_build(_Boom(), caplog)
    assert "kaboom from the provider" in caplog.text
    assert "Traceback" in caplog.text
