"""Tests for the `llm` tool: registration, dispatch, sandbox exposure.

The llm tool is the RLM-style primitive (arXiv:2512.24601): one auxiliary LLM
call, text -> text, usable as a normal tool call AND from inside execute_code
so a caller can fan semantic judgments over slices of a large input without
loading it into its own context window.
"""

import json
import types

import pytest


@pytest.fixture
def fresh_llm_tool():
    """Import the tool module cleanly per test (registry idempotency guard)."""
    import importlib
    import tools.llm_tool as mod
    importlib.reload(mod)
    return mod


class _FakeResponse:
    def __init__(self, text="ok"):
        self.choices = [types.SimpleNamespace(message=types.SimpleNamespace(content=text))]
        self.usage = types.SimpleNamespace(prompt_tokens=11, completion_tokens=7)


def test_llm_registered_in_registry(fresh_llm_tool):
    from tools.registry import registry
    assert registry._tools.get("llm") is not None, "llm must be a registered tool"
    entry = registry._tools["llm"]
    assert entry.toolset == "llm"
    assert entry.schema["name"] == "llm"
    params = entry.schema["parameters"]["properties"]
    assert {"prompt", "system", "max_tokens"} <= set(params)


def test_llm_in_core_tools_bundle():
    from toolsets import _HERMES_CORE_TOOLS
    assert "llm" in _HERMES_CORE_TOOLS, "llm must ship in the core bundle (every platform)"


def test_llm_available_inside_execute_code_sandbox():
    from tools.code_execution_tool import SANDBOX_ALLOWED_TOOLS, generate_hermes_tools_module
    assert "llm" in SANDBOX_ALLOWED_TOOLS
    stubs = generate_hermes_tools_module(sorted(SANDBOX_ALLOWED_TOOLS))
    assert "def llm(" in stubs, "sandbox hermes_tools module must expose llm()"


def test_handler_returns_json_text(fresh_llm_tool, monkeypatch):
    import agent.auxiliary_client as aux

    def fake_call_llm(*, messages, **kw):
        assert messages[-1]["role"] == "user"
        assert "juzga" in messages[-1]["content"]
        return _FakeResponse("SÍ")

    monkeypatch.setattr(aux, "call_llm", fake_call_llm)
    out = fresh_llm_tool.llm_tool(prompt="juzga esta nota")
    data = json.loads(out)
    assert data["success"] is True
    assert data["text"] == "SÍ"
    assert data["usage"]["prompt_tokens"] == 11


def test_handler_prepends_system_message(fresh_llm_tool, monkeypatch):
    import agent.auxiliary_client as aux
    seen = {}

    def fake_call_llm(*, messages, **kw):
        seen["roles"] = [m["role"] for m in messages]
        return _FakeResponse("x")

    monkeypatch.setattr(aux, "call_llm", fake_call_llm)
    fresh_llm_tool.llm_tool(prompt="p", system="sos un clasificador")
    assert seen["roles"] == ["system", "user"]


def test_handler_error_is_json_not_exception(fresh_llm_tool, monkeypatch):
    import agent.auxiliary_client as aux

    def boom(**kw):
        raise RuntimeError("provider caído")

    monkeypatch.setattr(aux, "call_llm", boom)
    out = fresh_llm_tool.llm_tool(prompt="p")
    data = json.loads(out)
    assert data["success"] is False
    assert "provider caído" in data["error"]


def test_handler_clamps_max_tokens(fresh_llm_tool, monkeypatch):
    import agent.auxiliary_client as aux
    seen = {}

    def fake_call_llm(*, max_tokens, **kw):
        seen["max_tokens"] = max_tokens
        return _FakeResponse("x")

    monkeypatch.setattr(aux, "call_llm", fake_call_llm)
    fresh_llm_tool.llm_tool(prompt="p", max_tokens=999_999)
    assert seen["max_tokens"] == fresh_llm_tool.MAX_OUTPUT_TOKENS
    fresh_llm_tool.llm_tool(prompt="p", max_tokens=5)
    assert seen["max_tokens"] == 5
