"""Unit tests for the Persistence helpers in api/v1/messages.py."""

from langchain_core.messages import AIMessage, HumanMessage

from app.api.v1.messages import _final_assistant_text


class _FakeState:
    def __init__(self, values):
        self.values = values


class _FakeAgent:
    def __init__(self, messages):
        self._messages = messages

    async def aget_state(self, config):
        return _FakeState({"messages": self._messages})


async def test_final_assistant_text_returns_last_ai_message():
    agent = _FakeAgent(
        [
            HumanMessage(content="hi"),
            AIMessage(content="plan reply"),
            AIMessage(content="ready"),
        ]
    )
    assert await _final_assistant_text(agent, {}) == "ready"


async def test_final_assistant_text_skips_non_text_blocks():
    agent = _FakeAgent(
        [
            AIMessage(
                content=[
                    {"type": "text", "text": "hello "},
                    {"type": "tool_use", "name": "f", "input": {}},
                ]
            )
        ]
    )
    assert await _final_assistant_text(agent, {}) == "hello "


async def test_final_assistant_text_empty_when_no_messages():
    agent = _FakeAgent([])
    assert await _final_assistant_text(agent, {}) == ""


async def test_final_assistant_text_empty_when_state_none():
    agent = _FakeAgent([])

    async def _none(config):
        return None

    agent.aget_state = _none
    assert await _final_assistant_text(agent, {}) == ""