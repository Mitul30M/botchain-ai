"""Runtime-contract tests for the n8n-mcp launch command and boot strictness.

These pin the WS9.3 behaviour: the MCP stdio server is launched with a
configurable command (the production image avoids `npx`), and a failure to list
tools is fatal by default rather than silently degrading the app.
"""

import pytest
from fastapi import FastAPI

import app.main as main_mod
from app.config import Settings


class _FakeCheckpointer:
    async def close(self) -> None:
        pass


class _FakeCkptService:
    checkpointer = _FakeCheckpointer()

    async def close(self) -> None:
        pass


class _FakeMCPClient:
    """Records how it was constructed; fails get_tools when `raise_on_tools`."""

    last_conn: dict | None = None
    raise_on_tools = False

    def __init__(self, conn: dict) -> None:
        type(self).last_conn = conn
        self.conn = conn

    async def get_tools(self) -> list:
        if type(self).raise_on_tools:
            raise RuntimeError("n8n-mcp tool listing failed")
        return ["search_nodes", "get_node"]


@pytest.fixture
def lifespan_env(monkeypatch):
    """Stub out every heavyweight lifespan dependency and yield the fakes."""
    _FakeMCPClient.last_conn = None
    _FakeMCPClient.raise_on_tools = False

    monkeypatch.setattr(main_mod, "setup_checkpoint", _no_ckpt)
    monkeypatch.setattr(main_mod, "MultiServerMCPClient", _FakeMCPClient)
    monkeypatch.setattr(main_mod, "create_model", lambda: "model")
    monkeypatch.setattr(main_mod, "create_agent", _fake_create_agent)
    return _FakeMCPClient


async def _no_ckpt():
    return _FakeCkptService()


def _fake_create_agent(model, tools, checkpointer, validate_connection=None):
    _fake_create_agent.tools = tools
    _fake_create_agent.validate_connection = validate_connection
    return "graph"


def _settings(**kw) -> Settings:
    return Settings(**kw)


def test_default_command_is_npx():
    assert _settings().n8n_mcp_command == "npx"


def test_connection_uses_settings_command():
    """The command is read from settings, not hardcoded."""
    conn = main_mod._n8n_mcp_connection(_settings(n8n_mcp_command="n8n-mcp"))
    assert conn["n8n-mcp"]["command"] == "n8n-mcp"
    assert conn["n8n-mcp"]["args"] == ["n8n-mcp"]


def test_connection_passes_env_to_mcp():
    conn = main_mod._n8n_mcp_connection(
        _settings(n8n_api_url="https://n8n.example", n8n_api_key="key")
    )
    env = conn["n8n-mcp"]["env"]
    assert env["N8N_API_URL"] == "https://n8n.example"
    assert env["N8N_API_KEY"] == "key"
    assert env["MCP_MODE"] == "stdio"


async def test_lifespan_builds_tools_and_agent(lifespan_env, monkeypatch):
    monkeypatch.setattr(main_mod, "get_settings", lambda: _settings())
    app = FastAPI()
    async with main_mod.lifespan(app):
        pass
    assert _fake_create_agent.tools == ["search_nodes", "get_node"]


async def test_lifespan_uses_the_same_command_for_validate(lifespan_env, monkeypatch):
    """`validate_connection` must use the identical connection the tools came from."""
    monkeypatch.setattr(main_mod, "get_settings", lambda: _settings(n8n_mcp_command="n8n-mcp"))
    app = FastAPI()
    async with main_mod.lifespan(app):
        pass
    assert _fake_create_agent.validate_connection["command"] == "n8n-mcp"
    assert lifespan_env.last_conn["n8n-mcp"]["command"] == "n8n-mcp"


async def test_lifespan_strict_boot_propagates_failure(lifespan_env, monkeypatch):
    """Default behaviour: a dead MCP server is a failed boot, not a degraded app."""
    lifespan_env.raise_on_tools = True
    monkeypatch.setattr(main_mod, "get_settings", lambda: _settings())
    with pytest.raises(RuntimeError, match="tool listing failed"):
        async with main_mod.lifespan(FastAPI()):
            pass


async def test_lifespan_non_strict_boot_continues(lifespan_env, monkeypatch):
    """Opt-in degradation: boot with an empty tool list rather than refusing to start."""
    lifespan_env.raise_on_tools = True
    monkeypatch.setattr(
        main_mod, "get_settings", lambda: _settings(n8n_mcp_strict_boot=False)
    )
    app = FastAPI()
    async with main_mod.lifespan(app):
        pass
    assert _fake_create_agent.tools == []


async def test_lifespan_closes_assets(lifespan_env, monkeypatch):
    monkeypatch.setattr(main_mod, "get_settings", lambda: _settings())
    app = FastAPI()
    async with main_mod.lifespan(app):
        pass
    assert app.state.mcp_client is None
    assert app.state.ckpt_service is not None


def test_env_overrides_command(monkeypatch):
    monkeypatch.setenv("N8N_MCP_COMMAND", "n8n-mcp")
    assert Settings().n8n_mcp_command == "n8n-mcp"


def test_env_parses_strict_boot_false(monkeypatch):
    monkeypatch.setenv("N8N_MCP_STRICT_BOOT", "false")
    assert Settings().n8n_mcp_strict_boot is False


def test_env_parses_strict_boot_true(monkeypatch):
    monkeypatch.setenv("N8N_MCP_STRICT_BOOT", "true")
    assert Settings().n8n_mcp_strict_boot is True