"""Unit tests for the canonical-shape normalizer in services/agent.py."""

import json

from app.services.agent import _normalize_workflow_json

# Exact shape the model produced for "Monday Pending Total to Slack":
# nodes keyed by id, connections keyed by id with targets by id, flat "main",
# positions as {"x","y"} objects.
OBJECT_KEYED = {
    "name": "WF",
    "nodes": {
        "1": {"parameters": {}, "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1, "name": "Schedule Trigger", "position": {"x": 200, "y": 100}},
        "2": {"parameters": {}, "type": "n8n-nodes-base.slack", "typeVersion": 2, "name": "Slack", "position": {"x": 500, "y": 100}},
    },
    "connections": {
        "1": {"main": [{"node": "2", "type": "main", "index": 0}]},
    },
    "settings": {"alwaysExpressPreviousNodeValue": True},
}

CANONICAL = {
    "name": "WF",
    "nodes": [
        {"parameters": {}, "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1, "name": "Schedule Trigger", "position": [200, 100]},
        {"parameters": {}, "type": "n8n-nodes-base.slack", "typeVersion": 2, "name": "Slack", "position": [500, 100]},
    ],
    "connections": {
        "Schedule Trigger": {"main": [[{"node": "Slack", "type": "main", "index": 0}]]},
    },
    "settings": {"alwaysExpressPreviousNodeValue": True},
}


def test_node_ids_and_connections_rewritten_to_canonical():
    assert _normalize_workflow_json(OBJECT_KEYED) == CANONICAL


def test_canonical_passes_through_unchanged():
    assert _normalize_workflow_json(CANONICAL) == CANONICAL


def test_nested_rewire_wraps_flat_main_entries():
    wf = {
        "nodes": {"a": {"id": "a", "name": "Start", "position": {"x": 0, "y": 0}}},
        "connections": {"a": {"main": [{"node": "a", "type": "main", "index": 1}]}},
    }
    out = _normalize_workflow_json(wf)
    assert out["nodes"] == [{"id": "a", "name": "Start", "position": [0, 0]}]
    assert out["connections"] == {
        "Start": {"main": [[{"node": "Start", "type": "main", "index": 1}]]}
    }


def test_untouched_when_target_not_a_node_key():
    wf = {
        "nodes": [{"name": "A", "position": [1, 2]}],
        "connections": {"A": {"main": [[{"node": "Something Else", "index": 0}]]}},
    }
    assert _normalize_workflow_json(wf) == wf


def test_input_not_mutated():
    original = json.dumps(OBJECT_KEYED, sort_keys=True)
    _normalize_workflow_json(OBJECT_KEYED)
    assert json.dumps(OBJECT_KEYED, sort_keys=True) == original


def test_missing_nodes_and_connections_is_noop():
    assert _normalize_workflow_json({"name": "x"}) == {"name": "x"}