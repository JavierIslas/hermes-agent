"""Tests del plugin arnes-gates.

Corre con: cd hermes-agent && source .venv/bin/activate && pytest tests/test_arnes_gates.py -v
(requiere que el plugin este en PYTHONPATH o accesible via el namespace
hermes_plugins — este test lo simula con el helper _load_plugin).

Cubre Fase 1: scaffold + analyze_scope + write gate + finish gate.
"""
import sys
import types
import importlib.util
from pathlib import Path

import pytest


# =============================================================================
# Helper: cargar el plugin como lo hace Hermes (namespace hermes_plugins).
# =============================================================================
PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent / "arnes-gates"


@pytest.fixture(autouse=True)
def _load_plugin(monkeypatch):
    """Carga el plugin en sys.modules bajo el namespace hermes_plugins.

    Cada test arranca con un modulo fresco + estado reseteado.
    """
    # Limpiar imports previos (por si un test anterior los dejo).
    for mod in list(sys.modules):
        if mod.startswith("hermes_plugins.arnes"):
            del sys.modules[mod]
    if "hermes_plugins" in sys.modules:
        del sys.modules["hermes_plugins"]

    # Crear el namespace padre.
    ns = types.ModuleType("hermes_plugins")
    ns.__path__ = []
    ns.__package__ = "hermes_plugins"
    sys.modules["hermes_plugins"] = ns

    # Cargar el plugin.
    module_name = "hermes_plugins.arnes_gates"
    spec = importlib.util.spec_from_file_location(
        module_name,
        str(PLUGIN_DIR / "__init__.py"),
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(PLUGIN_DIR)]
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    # Exponer el modulo para el test.
    monkeypatch.setattr(sys.modules[__name__], "_plugin", module, raising=False)
    yield
    # Limpieza.
    for mod in list(sys.modules):
        if mod.startswith("hermes_plugins"):
            del sys.modules[mod]


# =============================================================================
# Tests: analyze_scope (Task 1.2)
# =============================================================================
def test_analyze_scope_valido(_load_plugin):
    from hermes_plugins.arnes_gates import analyze_scope, gate_state
    gate_state.reset()
    result = analyze_scope(impact="BAJO", modules=["auth"], tests_status="covered")
    assert result.startswith("OK")
    assert gate_state.get()["scope_done"] is True


def test_analyze_scope_rechaza_none(_load_plugin):
    from hermes_plugins.arnes_gates import analyze_scope, gate_state
    gate_state.reset()
    result = analyze_scope(impact="BAJO", modules=["auth"], tests_status="none")
    assert "TDD" in result
    assert gate_state.get()["scope_done"] is False


def test_analyze_scope_impact_invalido(_load_plugin):
    from hermes_plugins.arnes_gates import analyze_scope, gate_state
    gate_state.reset()
    result = analyze_scope(impact="INVALIDO", modules=["auth"], tests_status="covered")
    assert "invalido" in result.lower()
    assert gate_state.get()["scope_done"] is False


def test_analyze_scope_modules_vacio(_load_plugin):
    from hermes_plugins.arnes_gates import analyze_scope, gate_state
    gate_state.reset()
    result = analyze_scope(impact="BAJO", modules=[], tests_status="covered")
    assert "no vacia" in result.lower()
    assert gate_state.get()["scope_done"] is False


# =============================================================================
# Tests: write gate (Task 1.3)
# =============================================================================
def test_write_blocked_without_scope(_load_plugin):
    from hermes_plugins.arnes_gates import gate_state, _on_pre_tool_call
    gate_state.reset()
    result = _on_pre_tool_call("write_file", {"path": "/tmp/x.py", "content": "x=1"})
    assert result is not None
    assert result["action"] == "block"
    assert "scope" in result["message"].lower()


def test_write_blocked_without_scope_patch(_load_plugin):
    from hermes_plugins.arnes_gates import gate_state, _on_pre_tool_call
    gate_state.reset()
    result = _on_pre_tool_call("patch", {"path": "/tmp/x.py"})
    assert result is not None
    assert result["action"] == "block"


def test_write_allowed_with_scope(_load_plugin, tmp_path):
    from hermes_plugins.arnes_gates import analyze_scope, gate_state, _on_pre_tool_call
    gate_state.reset()
    analyze_scope(impact="BAJO", modules=["auth"], tests_status="covered")
    # Archivo nuevo: se permite crear.
    new_file = tmp_path / "nuevo.py"
    result = _on_pre_tool_call("write_file", {"path": str(new_file), "content": "x=1"})
    assert result is None  # permitido


def test_write_blocked_unread_existing(_load_plugin, tmp_path):
    """No pisar archivo existente sin leerlo antes."""
    from hermes_plugins.arnes_gates import analyze_scope, gate_state, _on_pre_tool_call
    gate_state.reset()
    existing = tmp_path / "existe.py"
    existing.write_text("x = 1\n")
    analyze_scope(impact="BAJO", modules=["auth"], tests_status="covered")
    result = _on_pre_tool_call("write_file", {"path": str(existing), "content": "x=2"})
    assert result is not None
    assert result["action"] == "block"
    assert "leido" in result["message"].lower()


def test_write_allowed_after_read(_load_plugin, tmp_path):
    """Si se leyo el archivo, se permite escribir."""
    from hermes_plugins.arnes_gates import analyze_scope, gate_state, _on_pre_tool_call
    gate_state.reset()
    existing = tmp_path / "existe.py"
    existing.write_text("x = 1\n")
    analyze_scope(impact="BAJO", modules=["auth"], tests_status="covered")
    # Simular que se leyo.
    _on_pre_tool_call("read_file", {"path": str(existing)})
    result = _on_pre_tool_call("write_file", {"path": str(existing), "content": "x=2"})
    assert result is None  # permitido porque ya se leyo


# =============================================================================
# Tests: finish gate / pre_verify (Task 1.4)
# =============================================================================
def test_finish_blocked_with_failed_tests(_load_plugin):
    """Finish gate bloquea cuando hay evidence de tests fallidos."""
    from hermes_plugins.arnes_gates import analyze_scope, gate_state, _on_pre_verify
    gate_state.reset()
    analyze_scope(impact="BAJO", modules=["auth"], tests_status="covered")
    # Simular output de tests fallidos (evidence real).
    gate_state.get()["last_test_output"] = "=== 3 failed in 0.5s ==="
    result = _on_pre_verify(coding=True, changed_paths=["auth.py"])
    assert result is not None
    assert result["action"] == "continue"
    assert "fail" in result["message"].lower() or "fall" in result["message"].lower()


def test_finish_pass_with_green_tests(_load_plugin):
    """Finish gate deja cerrar cuando hay evidence de tests pasando."""
    from hermes_plugins.arnes_gates import analyze_scope, gate_state, _on_pre_verify
    gate_state.reset()
    analyze_scope(impact="BAJO", modules=["auth"], tests_status="covered")
    # Simular output de tests pasando.
    gate_state.get()["last_test_output"] = "=== 10 passed in 0.5s ==="
    result = _on_pre_verify(coding=True, changed_paths=["auth.py"])
    assert result is None  # deja cerrar


def test_finish_fail_open_no_test_output(_load_plugin):
    """Finish gate NO bloquea cuando no hay evidence de tests (fail-open)."""
    from hermes_plugins.arnes_gates import analyze_scope, gate_state, _on_pre_verify
    gate_state.reset()
    analyze_scope(impact="BAJO", modules=["auth"], tests_status="covered")
    # No setear last_test_output → evidence fail_open.
    result = _on_pre_verify(coding=True, changed_paths=["auth.py"])
    assert result is None  # fail-open: deja cerrar


def test_finish_blocked_without_scope(_load_plugin):
    from hermes_plugins.arnes_gates import gate_state, _on_pre_verify
    gate_state.reset()
    result = _on_pre_verify(coding=True, changed_paths=["x.py"])
    assert result is not None
    assert "scope" in result["message"].lower()


def test_finish_skips_non_coding(_load_plugin):
    """Si coding=False (no toca codigo), el gate no aplica."""
    from hermes_plugins.arnes_gates import gate_state, _on_pre_verify
    gate_state.reset()
    result = _on_pre_verify(coding=False, changed_paths=[])
    assert result is None  # deja cerrar


def test_finish_skips_no_changed_paths(_load_plugin):
    from hermes_plugins.arnes_gates import gate_state, _on_pre_verify
    gate_state.reset()
    result = _on_pre_verify(coding=True, changed_paths=[])
    assert result is None


def test_finish_allows_with_all_green(_load_plugin):
    """Todo verde: deja cerrar y prende done."""
    from hermes_plugins.arnes_gates import analyze_scope, gate_state, _on_pre_verify
    gate_state.reset()
    analyze_scope(impact="BAJO", modules=["auth"], tests_status="covered")
    gate = gate_state.get()
    gate["tests_green"] = True
    gate["lint_green"] = True
    gate["typecheck_green"] = True
    result = _on_pre_verify(coding=True, changed_paths=["auth.py"])
    assert result is None
    assert gate_state.get()["done"] is True
