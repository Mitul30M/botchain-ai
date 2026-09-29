"""Phase 7 — sandbox lifecycle + DB-backed workflow delivery.

Pins the phase gate "the workflow survives the request via DB, not disk":
the per-build sandbox is ephemeral scratch that is torn down when the build
node returns, while the final workflow JSON lives on in Message.meta and is
served by the download route without any file on disk.
"""

import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

import app.services.agent as agent_mod
from app.api.v1.messages import router as messages_router
from app.db import get_session
from app.deps import get_owned_chat
from app.models import Attachment, Message
from app.services.agent import _make_build_node, _make_write_json_tool


class _RouteChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _RouteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _DownloadSession:
    def __init__(self, rows):
        self._rows = list(rows)
        self._i = 0

    async def execute(self, stmt):
        row = self._rows[self._i] if self._i < len(self._rows) else None
        self._i += 1
        return _RouteResult([row] if row is not None else [])


def _make_download_app(session, chat):
    app = FastAPI()
    app.include_router(messages_router)
    app.dependency_overrides[get_owned_chat] = lambda: chat
    app.dependency_overrides[get_session] = lambda: session
    return app


def _message_with_workflow():
    return Message(
        id="m-1",
        chat_id="chat-1",
        role="assistant",
        meta={"phase": "done", "validation": {"status": "valid", "errors": []},
              "workflow_json": {"name": "Webhook and Slack", "nodes": []}},
    )


def _attachment():
    return Attachment(
        id="a-1",
        message_id="m-1",
        file_name="workflow.json",
        file_type="application/json",
        file_url="/api/v1/chats/chat-1/messages/m-1/attachments/a-1/download",
        size_bytes=42,
    )


def test_download_serves_workflow_from_meta_never_disk():
    session = _DownloadSession([_message_with_workflow(), _attachment()])
    app = _make_download_app(session, _RouteChat("chat-1"))

    with TestClient(app) as client:
        response = client.get(
            "/chat-1/messages/m-1/attachments/a-1/download"
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["content-disposition"] == (
        'attachment; filename="workflow.json"'
    )
    assert json.loads(response.text)["name"] == "Webhook and Slack"


def test_download_404_when_message_missing():
    session = _DownloadSession([None, None])
    app = _make_download_app(session, _RouteChat("chat-1"))

    with TestClient(app) as client:
        response = client.get(
            "/chat-1/messages/m-1/attachments/a-1/download"
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Message not found"


def test_download_404_when_attachment_missing():
    session = _DownloadSession([_message_with_workflow(), None])
    app = _make_download_app(session, _RouteChat("chat-1"))

    with TestClient(app) as client:
        response = client.get(
            "/chat-1/messages/m-1/attachments/a-1/download"
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Attachment not found"


def test_download_404_when_workflow_missing_from_meta():
    message = Message(
        id="m-1",
        chat_id="chat-1",
        role="assistant",
        meta={"phase": "done", "validation": {"status": "valid", "errors": []}},
    )
    session = _DownloadSession([message, _attachment()])
    app = _make_download_app(session, _RouteChat("chat-1"))

    with TestClient(app) as client:
        response = client.get(
            "/chat-1/messages/m-1/attachments/a-1/download"
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Workflow payload not available"


class _BuildModel:
    def __init__(self, replies):
        self._replies = list(replies)
        self._i = 0

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        reply = self._replies[self._i]
        self._i += 1
        return reply


def _noop_writer():
    def writer(payload):
        return None

    return writer


async def test_build_sandbox_dir_removed_after_build(monkeypatch):
    monkeypatch.setattr(agent_mod, "get_stream_writer", _noop_writer)
    captured = {}

    def wrapped(sandbox_dir):
        captured["dir"] = sandbox_dir
        return _make_write_json_tool(sandbox_dir)

    monkeypatch.setattr(agent_mod, "_make_write_json_tool", wrapped)

    model = _BuildModel(
        [
            AIMessage(
                content="assembling",
                tool_calls=[{
                    "name": "write_json_file",
                    "args": {
                        "file_path": "workflow.json",
                        "content": {"name": "Webhook and Slack"},
                    },
                    "id": "tc-1",
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="done"),
        ]
    )
    node = _make_build_node(model, {}, None)

    result = await node({"messages": [HumanMessage(content="build it")]})

    assert result["phase"] == "build"
    assert result["workflow_json"]["name"] == "Webhook and Slack"
    assert not os.path.isdir(captured["dir"])


def test_write_tool_writes_within_sandbox(tmp_path):
    tool = _make_write_json_tool(str(tmp_path))
    out = tool.invoke({
        "file_path": "workflow.json",
        "content": {"name": "Webhook and Slack"},
    })
    assert out.startswith("Updated file")
    with open(tmp_path / "workflow.json", encoding="utf-8") as fh:
        assert json.load(fh)["name"] == "Webhook and Slack"


def test_write_tool_rejects_path_escape(tmp_path):
    tool = _make_write_json_tool(str(tmp_path))
    with pytest.raises(RuntimeError, match="escapes the sandbox"):
        tool.invoke({
            "file_path": "../evil.json",
            "content": {"name": "nope"},
        })
    assert not (tmp_path.parent / "evil.json").exists()


def test_write_tool_rejects_non_json_content(tmp_path):
    tool = _make_write_json_tool(str(tmp_path))
    with pytest.raises(RuntimeError, match="must be a JSON object"):
        tool.invoke({"file_path": "workflow.json", "content": "not json"})