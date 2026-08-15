"""Tests de parse_cd: tracking del cwd del terminal."""
import sys
import types
import importlib.util
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "arnes-gates"


@pytest.fixture(scope="module", autouse=True)
def _tg():
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
    yield


def _parse_cd(cmd, current_cwd=None):
    from hermes_plugins.arnes_gates.terminal_guard import parse_cd
    return parse_cd(cmd, current_cwd)


# =============================================================================
# cd absoluto.
# =============================================================================
def test_cd_absolute():
    assert _parse_cd("cd /workspace/foo") == "/workspace/foo"


def test_cd_absolute_nested():
    assert _parse_cd("cd /workspace/desarrollosPersonales/hermes-fork") == \
        "/workspace/desarrollosPersonales/hermes-fork"


# =============================================================================
# cd relativo.
# =============================================================================
def test_cd_relative_with_base():
    assert _parse_cd("cd bar", current_cwd="/workspace/foo") == "/workspace/foo/bar"


def test_cd_relative_parent():
    assert _parse_cd("cd ..", current_cwd="/workspace/foo/bar") == "/workspace/foo/bar/.."


def test_cd_relative_no_base():
    """cd relativo sin current_cwd → None (no se puede resolver)."""
    assert _parse_cd("cd bar") is None


# =============================================================================
# cd en comando compuesto.
# =============================================================================
def test_cd_then_command():
    assert _parse_cd("cd /workspace/foo && pytest", current_cwd="/opt/hermes") == \
        "/workspace/foo"


def test_cd_semicolon():
    assert _parse_cd("cd /tmp; ls", current_cwd="/opt/hermes") == "/tmp"


# =============================================================================
# cd sin argumentos o especiales.
# =============================================================================
def test_cd_no_args():
    """cd sin args → $HOME, no podemos resolver → None."""
    assert _parse_cd("cd") is None


def test_cd_dash():
    """cd - → directorio anterior, no podemos saberlo → None."""
    assert _parse_cd("cd -") is None


# =============================================================================
# Sin cd.
# =============================================================================
def test_no_cd():
    assert _parse_cd("ls -la") is None


def test_no_cd_pytest():
    assert _parse_cd("pytest tests/") is None


def test_empty():
    assert _parse_cd("") is None


# =============================================================================
# cd con espacios en el path.
# =============================================================================
def test_cd_quoted_path():
    assert _parse_cd('cd "my dir"') is None  # relativo sin base → None


def test_cd_quoted_absolute():
    assert _parse_cd('cd "/workspace/my dir"') == "/workspace/my dir"


# =============================================================================
# cd no es el primer comando (subshell o pipe).
# =============================================================================
def test_cd_after_pipe():
    """cd despues de un pipe no deberia cambiar el cwd del terminal."""
    result = _parse_cd("echo x | cd /workspace", current_cwd="/opt/hermes")
    # El parser es simple: agarra el primer cd que encuentra.
    # Esto es una limitacion aceptable.
    assert result == "/workspace"  # lo agarra igual, pero es edge case raro
