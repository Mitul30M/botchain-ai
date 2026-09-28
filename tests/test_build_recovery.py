"""Unit tests for the build-step JSON recovery helpers in services/agent.py."""

from app.services.agent import _coerce_json_content, _extract_json_object


def test_coerce_accepts_dict_and_list():
    obj = {"name": "wf", "nodes": []}
    assert _coerce_json_content(obj) is obj
    lst = [obj]
    assert _coerce_json_content(lst) is lst


def test_coerce_parses_json_string():
    assert _coerce_json_content('{"name": "wf"}') == {"name": "wf"}


def test_coerce_rejects_garbage_string_and_scalars():
    assert _coerce_json_content("not json") is None
    assert _coerce_json_content(42) is None
    assert _coerce_json_content(None) is None


def test_extract_json_from_pure_payload():
    assert _extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_from_fenced_block():
    text = "```json\n{\"a\": 1}\n```"
    assert _extract_json_object(text) == {"a": 1}


def test_extract_json_from_prose_wrapper():
    text = 'Here is the workflow: {"name": "Webhook to Slack", "nodes": []}'
    assert _extract_json_object(text) == {"name": "Webhook to Slack", "nodes": []}


def test_extract_json_returns_none_when_absent():
    assert _extract_json_object("No workflow — please ask a question instead.") is None
    assert _extract_json_object("") is None