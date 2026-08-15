"""Tests de Q5: commit gate — git commit verificado."""
import sys
import types
import importlib.util
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "arnes-gates"


@pytest.fixture(autouse=True)
def _load_and_reset():
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        ns.__package__ = "hermes_plugins"
        sys.modules["hermes_plugins"] = ns
    if "hermes_plugins.arnes_gates" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "hermes_plugins.arnes_gates",
            str(PLUGIN_DIR / "__init__.py"),
            submodule_search_locations=[str(PLUGIN_DIR)],
        )
        module = importlib.util.module_from_spec(spec)
        module.__package__ = "hermes_plugins.arnes_gates"
        module.__path__ = [str(PLUGIN_DIR)]
        sys.modules["hermes_plugins.arnes_gates"] = module
        spec.loader.exec_module(module)
    from hermes_plugins.arnes_gates import gate_state
    gate_state.reset()
    yield
    gate_state.reset()


# =============================================================================
# detect_git_commit — detector.
# =============================================================================
def test_detect_git_commit_simple():
    from hermes_plugins.arnes_gates.terminal_guard import detect_git_commit
    assert detect_git_commit("git commit -m 'msg'") is True


def test_detect_git_commit_with_message():
    from hermes_plugins.arnes_gates.terminal_guard import detect_git_commit
    assert detect_git_commit("git commit -m 'fix: arregla bug'") is True


def test_detect_git_commit_amend():
    from hermes_plugins.arnes_gates.terminal_guard import detect_git_commit
    assert detect_git_commit("git commit --amend --no-edit") is True


def test_detect_git_not_commit():
    """git status, git push, etc. NO son commit."""
    from hermes_plugins.arnes_gates.terminal_guard import detect_git_commit
    assert detect_git_commit("git status") is False
    assert detect_git_commit("git push") is False
    assert detect_git_commit("git log --oneline") is False
    assert detect_git_commit("git add .") is False


def test_detect_git_commit_empty():
    from hermes_plugins.arnes_gates.terminal_guard import detect_git_commit
    assert detect_git_commit("") is False
    assert detect_git_commit(None) is False


def test_detect_git_commit_in_pipe():
    from hermes_plugins.arnes_gates.terminal_guard import detect_git_commit
    assert detect_git_commit("echo x | git commit -m 'y'") is True


# =============================================================================
# Commit gate — bloqueo sin done.
# =============================================================================
def test_commit_blocked_without_done():
    """git commit sin done=True → BLOQUEADO."""
    from hermes_plugins.arnes_gates import _check_pre_tool_call, gate_state
    gate_state.get()["done"] = False
    result = _check_pre_tool_call(
        tool_name="terminal",
        args={"command": "git commit -m 'test'"},
    )
    assert result is not None
    assert result["action"] == "block"
    assert "finish" in result["message"].lower()


def test_commit_allowed_with_done_no_dangling():
    """git commit con done=True y sin indice → permite (fail-open dangling)."""
    from hermes_plugins.arnes_gates import _check_pre_tool_call, gate_state
    from hermes_plugins.arnes_gates import index_service
    index_service._index = None
    index_service._project_root = None
    gate_state.get()["done"] = True
    gate_state.get()["scope_done"] = True
    result = _check_pre_tool_call(
        tool_name="terminal",
        args={"command": "git commit -m 'test'"},
    )
    # Sin indice → no verifica dangling → permite.
    assert result is None


def test_commit_not_triggered_by_other_commands():
    """Comandos que no son git commit no activan el gate."""
    from hermes_plugins.arnes_gates import _check_pre_tool_call, gate_state
    gate_state.get()["done"] = False
    gate_state.get()["scope_done"] = True
    result = _check_pre_tool_call(
        tool_name="terminal",
        args={"command": "git status"},
    )
    assert result is None  # no es commit, no bloquea
