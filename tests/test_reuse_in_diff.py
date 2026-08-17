"""Tests de validar_reuse_en_diff: uso real del reuse en el código escrito."""
import sys
import types
import importlib.util
import tempfile
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


def _set_plan_reuse(names):
    """Setea last_plan con reuse citando los nombres dados."""
    from hermes_plugins.arnes_gates import gate_state
    gate_state.get()["last_plan"] = {
        "reuse": [{"name": n} for n in names]
    }


# =============================================================================
# Casos basicos.
# =============================================================================
def test_no_reuse_in_plan():
    """Sin reuse en el plan → None (no aplica)."""
    from hermes_plugins.arnes_gates.reuse_gates import validar_reuse_en_diff
    result = validar_reuse_en_diff("x = 1", set())
    assert result is None


def test_reuse_used_as_name():
    """Símbolo citado aparece como ast.Name → None (OK)."""
    from hermes_plugins.arnes_gates.reuse_gates import validar_reuse_en_diff
    _set_plan_reuse(["foo"])
    result = validar_reuse_en_diff("result = foo(x)", set())
    assert result is None


def test_reuse_used_as_attribute():
    """Símbolo citado aparece como ast.Attribute → None (OK)."""
    from hermes_plugins.arnes_gates.reuse_gates import validar_reuse_en_diff
    _set_plan_reuse(["bar"])
    result = validar_reuse_en_diff("obj.bar(x)", set())
    assert result is None


def test_reuse_not_used():
    """Símbolo citado NO aparece en el código → BLOQUEADO."""
    from hermes_plugins.arnes_gates.reuse_gates import validar_reuse_en_diff
    _set_plan_reuse(["foo"])
    result = validar_reuse_en_diff("x = 1 + 2", set())
    assert result is not None
    assert "BLOQUEADO" in result
    assert "foo" in result


def test_reuse_in_comment_not_counted():
    """Símbolo solo en comentario → BLOQUEADO (no es uso sintáctico)."""
    from hermes_plugins.arnes_gates.reuse_gates import validar_reuse_en_diff
    _set_plan_reuse(["foo"])
    result = validar_reuse_en_diff("# uses foo\nx = 1", set())
    assert result is not None
    assert "BLOQUEADO" in result


def test_reuse_in_string_not_counted():
    """Símbolo solo en string → BLOQUEADO (no es uso sintáctico)."""
    from hermes_plugins.arnes_gates.reuse_gates import validar_reuse_en_diff
    _set_plan_reuse(["foo"])
    result = validar_reuse_en_diff('msg = "uses foo here"', set())
    assert result is not None
    assert "BLOQUEADO" in result


# =============================================================================
# Diff acumulado (archivos ya escritos).
# =============================================================================
def test_reuse_in_previously_written_file():
    """Símbolo usado en un archivo ya escrito → OK (diff acumulado)."""
    from hermes_plugins.arnes_gates.reuse_gates import validar_reuse_en_diff
    _set_plan_reuse(["foo"])
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("result = foo(x)\n")
        f.flush()
        prev_path = f.name
    try:
        result = validar_reuse_en_diff("x = 1", {prev_path})
        assert result is None
    finally:
        Path(prev_path).unlink(missing_ok=True)


def test_reuse_not_anywhere():
    """Símbolo no en content ni en archivos previos → BLOQUEADO."""
    from hermes_plugins.arnes_gates.reuse_gates import validar_reuse_en_diff
    _set_plan_reuse(["foo"])
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("x = 1\n")
        f.flush()
        prev_path = f.name
    try:
        result = validar_reuse_en_diff("y = 2", {prev_path})
        assert result is not None
        assert "foo" in result
    finally:
        Path(prev_path).unlink(missing_ok=True)


# =============================================================================
# Casos multiples.
# =============================================================================
def test_reuse_multiple_all_used():
    """Multiples símbolos citados, todos usados → OK."""
    from hermes_plugins.arnes_gates.reuse_gates import validar_reuse_en_diff
    _set_plan_reuse(["foo", "bar"])
    result = validar_reuse_en_diff("a = foo(x)\nb = bar(y)", set())
    assert result is None


def test_reuse_multiple_one_missing():
    """Multiples símbolos citados, uno falta → BLOQUEADO."""
    from hermes_plugins.arnes_gates.reuse_gates import validar_reuse_en_diff
    _set_plan_reuse(["foo", "bar"])
    result = validar_reuse_en_diff("a = foo(x)", set())
    assert result is not None
    assert "bar" in result
    assert "foo" not in result.split("pero")[0]  # foo no está en los faltantes


# =============================================================================
# Qualname (nombre con punto).
# =============================================================================
def test_reuse_qualname_simple_name_used():
    """Reuse con qualname 'module.func' → busca 'func' como uso."""
    from hermes_plugins.arnes_gates.reuse_gates import validar_reuse_en_diff
    from hermes_plugins.arnes_gates import gate_state
    gate_state.get()["last_plan"] = {"reuse": [{"name": "mymod.helper_func"}]}
    result = validar_reuse_en_diff("x = helper_func(1)", set())
    assert result is None


# =============================================================================
# Syntax error en content.
# =============================================================================
def test_syntax_error_content():
    """Content con syntax error → _nombres_referenciados devuelve set vacio.
    Si hay reuse citado → BLOQUEADO (no se encontro uso).
    Si no hay reuse → None (no aplica)."""
    from hermes_plugins.arnes_gates.reuse_gates import validar_reuse_en_diff
    _set_plan_reuse(["foo"])
    result = validar_reuse_en_diff("def broken(:", set())
    assert result is not None
    assert "foo" in result
