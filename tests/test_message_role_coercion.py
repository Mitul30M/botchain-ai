"""Unit tests for last-role coercion in services/agent.py.

Mistral rejects a request whose final message is an assistant or system turn
(or an assistant turn with tool_calls that has no matching tool results) —
the approve→build resumption produces exactly such a history, so the build
node must coerce it to end on a user/tool role before invoking the model.
"""

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.services.agent import _ensure_user_last


def test_strips_trailing_assistant_messages():
    history = [
        HumanMessage(content="Build a webhook."),
        AIMessage(content="Got it — building your workflow now."),
    ]
    result = _ensure_user_last(history)
    assert [type(m) for m in result] == [HumanMessage]


def test_strips_multiple_trailing_assistant_and_system():
    history = [
        HumanMessage(content="hi"),
        AIMessage(content="summary"),
        AIMessage(content="got it"),
        SystemMessage(content="spec"),
    ]
    result = _ensure_user_last(history)
    assert len(result) == 1
    assert isinstance(result[0], HumanMessage)


def test_keeps_history_ending_on_user_or_tool():
    user_end = [AIMessage(content="x"), HumanMessage(content="hi")]
    tool_end = [
        AIMessage(content="", tool_calls=[{"name": "f", "args": {}, "id": "1", "type": "tool_call"}]),
        ToolMessage(content="ok", tool_call_id="1"),
    ]
    assert [type(m) for m in _ensure_user_last(user_end)] == [
        AIMessage,
        HumanMessage,
    ]
    assert [type(m) for m in _ensure_user_last(tool_end)] == [
        AIMessage,
        ToolMessage,
    ]


def test_never_returns_empty_history():
    result = _ensure_user_last([SystemMessage(content="only system")])
    assert len(result) == 1
    assert isinstance(result[0], HumanMessage)
    assert result[0].content