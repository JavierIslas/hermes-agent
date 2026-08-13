"""Tests de validar_reuse: verificar firmas citadas contra el indice real."""
import sys
import types
import importlib.util
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent / "arnes-gates"


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


# Mini-índice con dos símbolos: foo(x) y bar(a, b) -> str
MOCK_INDEX = {
    "version": 4,
    "root": "/fake",
    "files": {
        "mod.py": {
            "path": "mod.py",
            "symbols": [
                {
                    "name": "foo",
                    "qualname": "foo",
                    "kind": "function",
                    "lineno": 1,
                    "end_lineno": 2,
                    "signature": "foo(x)",
                    "params": [{"name": "x", "type": None}],
                    "returns": None,
                    "struct_hash": None,
                },
                {
                    "name": "bar",
                    "qualname": "bar",
                    "kind": "function",
                    "lineno": 5,
                    "end_lineno": 7,
                    "signature": "bar(a, b) -> str",
                    "params": [
                        {"name": "a", "type": None},
                        {"name": "b", "type": None},
                    ],
                    "returns": "str",
                    "struct_hash": None,
                },
            ],
        }
    },
}


def _set_index(idx):
    """Inyecta un indice mock en index_service."""
    from hermes_plugins.arnes_gates import index_service
    index_service._index = idx
    index_service._project_root = Path("/fake")


def _clear_index():
    from hermes_plugins.arnes_gates import index_service
    index_service._index = None
    index_service._project_root = None


# =============================================================================
# Casos basicos sin indice (fail-open).
# =============================================================================
def test_reuse_empty_ok():
    """Reuse vacio → scope aprobado."""
    from hermes_plugins.arnes_gates import analyze_scope
    result = analyze_scope(impact="BAJO", modules=["auth"], tests_status="covered")
    assert "OK" in result


def test_reuse_no_index_fail_open():
    """Sin indice cargado → reuse se acepta sin verificar (fail-open)."""
    from hermes_plugins.arnes_gates import analyze_scope
    _clear_index()
    result = analyze_scope(
        impact="BAJO", modules=["auth"], tests_status="covered",
        reuse=[{"name": "inexistente"}],
    )
    assert "OK" in result  # fail-open: no bloquea


# =============================================================================
# Casos con indice cargado.
# =============================================================================
def test_reuse_correct_signature():
    """Reuse con firma correcta → scope aprobado."""
    from hermes_plugins.arnes_gates import analyze_scope
    _set_index(MOCK_INDEX)
    result = analyze_scope(
        impact="BAJO", modules=["auth"], tests_status="covered",
        reuse=[{"name": "foo", "params": [{"name": "x"}]}],
    )
    assert "OK" in result


def test_reuse_nonexistent_symbol():
    """Reuse con simbolo inexistente → ERROR."""
    from hermes_plugins.arnes_gates import analyze_scope
    _set_index(MOCK_INDEX)
    result = analyze_scope(
        impact="BAJO", modules=["auth"], tests_status="covered",
        reuse=[{"name": "no_existe"}],
    )
    assert "ERROR" in result
    assert "no existe" in result.lower() or "no_existe" in result


def test_reuse_wrong_arity():
    """Reuse con aridad distinta → ERROR."""
    from hermes_plugins.arnes_gates import analyze_scope
    _set_index(MOCK_INDEX)
    result = analyze_scope(
        impact="BAJO", modules=["auth"], tests_status="covered",
        reuse=[{"name": "foo", "params": [{"name": "x"}, {"name": "y"}]}],
    )
    assert "ERROR" in result
    assert "no calza" in result.lower() or "calza" in result.lower()


def test_reuse_correct_with_return():
    """Reuse con return type correcto → OK."""
    from hermes_plugins.arnes_gates import analyze_scope
    _set_index(MOCK_INDEX)
    result = analyze_scope(
        impact="BAJO", modules=["auth"], tests_status="covered",
        reuse=[{"name": "bar", "params": [{"name": "a"}, {"name": "b"}], "returns": "str"}],
    )
    assert "OK" in result


def test_reuse_multiple_correct():
    """Reuse multiple con todas las firmas correctas → OK."""
    from hermes_plugins.arnes_gates import analyze_scope
    _set_index(MOCK_INDEX)
    result = analyze_scope(
        impact="BAJO", modules=["auth"], tests_status="covered",
        reuse=[
            {"name": "foo", "params": [{"name": "x"}]},
            {"name": "bar", "params": [{"name": "a"}, {"name": "b"}], "returns": "str"},
        ],
    )
    assert "OK" in result


def test_reuse_mixed_correct_and_wrong():
    """Reuse con uno correcto y uno inexistente → ERROR."""
    from hermes_plugins.arnes_gates import analyze_scope
    _set_index(MOCK_INDEX)
    result = analyze_scope(
        impact="BAJO", modules=["auth"], tests_status="covered",
        reuse=[
            {"name": "foo", "params": [{"name": "x"}]},
            {"name": "inexistente"},
        ],
    )
    assert "ERROR" in result
    assert "inexistente" in result


def test_reuse_shows_cited_vs_real():
    """ERROR de firma muestra citada vs real."""
    from hermes_plugins.arnes_gates import analyze_scope
    _set_index(MOCK_INDEX)
    result = analyze_scope(
        impact="BAJO", modules=["auth"], tests_status="covered",
        reuse=[{"name": "foo", "params": [{"name": "x"}, {"name": "y"}]}],
    )
    assert "ERROR" in result
    assert "citada" in result.lower()
    assert "real" in result.lower()


def test_reuse_struct_invalid():
    """Reuse item sin 'name' → ERROR."""
    from hermes_plugins.arnes_gates import analyze_scope
    _set_index(MOCK_INDEX)
    result = analyze_scope(
        impact="BAJO", modules=["auth"], tests_status="covered",
        reuse=[{"params": [{"name": "x"}]}],  # sin name
    )
    assert "ERROR" in result
    assert "name" in result.lower()


def test_reuse_not_a_list():
    """Reuse que no es lista → se valida como dict sin name → ERROR."""
    from hermes_plugins.arnes_gates import analyze_scope
    _set_index(MOCK_INDEX)
    result = analyze_scope(
        impact="BAJO", modules=["auth"], tests_status="covered",
        reuse=[42],  # no es dict
    )
    assert "ERROR" in result
