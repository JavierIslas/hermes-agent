"""Tests del circuit breaker: detección de loops de bloqueos del arnés."""
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


# =============================================================================
# Circuit breaker básico.
# =============================================================================
def test_circuit_trips_after_5_blocks():
    """Después de 5 bloqueos consecutivos, el circuit breaker activa."""
    from hermes_plugins.arnes_gates import _on_pre_tool_call, gate_state

    # 5 intentos de write_file sin scope → 5 bloqueos.
    for i in range(5):
        result = _on_pre_tool_call(tool_name="write_file", args={"path": f"/tmp/test_{i}.py", "content": "x"})
        assert result is not None
        assert result["action"] == "block"

    # El 5º bloqueo debería haber trippeado el circuit breaker.
    assert gate_state.get()["circuit_tripped"] is True
    assert gate_state.get()["block_count"] >= 5


def test_circuit_blocks_all_tools_after_trip():
    """Una vez tripped, TODAS las tools se bloquean (incluso read_file)."""
    from hermes_plugins.arnes_gates import _on_pre_tool_call, gate_state

    # Trippear el circuit breaker.
    gate_state.get()["circuit_tripped"] = True

    # read_file (que normalmente siempre pasa) ahora se bloquea.
    result = _on_pre_tool_call(tool_name="read_file", args={"path": "/tmp/foo.py"})
    assert result is not None
    assert result["action"] == "block"
    assert "CIRCUIT BREAKER" in result["message"]


def test_circuit_resets_on_successful_tool():
    """Una tool exitosa entre los bloqueos resetea el contador."""
    from hermes_plugins.arnes_gates import _on_pre_tool_call, gate_state

    # 3 bloqueos.
    for i in range(3):
        _on_pre_tool_call(tool_name="write_file", args={"path": f"/tmp/test_{i}.py", "content": "x"})

    assert gate_state.get()["block_count"] == 3

    # Una tool exitosa (read_file no se bloquea).
    _on_pre_tool_call(tool_name="read_file", args={"path": "/tmp/foo.py"})

    # El contador resetea a 0.
    assert gate_state.get()["block_count"] == 0
    assert gate_state.get()["circuit_tripped"] is False


def test_circuit_resets_on_session_start():
    """El contador se resetea en on_session_start."""
    from hermes_plugins.arnes_gates import gate_state, _on_session_start

    gate_state.get()["circuit_tripped"] = True
    gate_state.get()["block_count"] = 10

    _on_session_start()

    assert gate_state.get()["circuit_tripped"] is False
    assert gate_state.get()["block_count"] == 0


def test_circuit_does_not_trip_with_progress():
    """Si entre los bloqueos hay tools exitosas, el circuit no trippea."""
    from hermes_plugins.arnes_gates import _on_pre_tool_call, gate_state

    # Alternar bloqueos con éxitos.
    for i in range(10):
        # Bloqueo (write sin scope).
        _on_pre_tool_call(tool_name="write_file", args={"path": f"/tmp/test_{i}.py", "content": "x"})
        # Éxito (read_file pasa).
        _on_pre_tool_call(tool_name="read_file", args={"path": "/tmp/foo.py"})

    # El contador nunca llegó a 5 consecutivos.
    assert gate_state.get()["circuit_tripped"] is False


def test_block_count_increments():
    """Cada bloqueo incrementa block_count en 1."""
    from hermes_plugins.arnes_gates import _on_pre_tool_call, gate_state

    for i in range(4):
        result = _on_pre_tool_call(tool_name="write_file", args={"path": f"/tmp/test_{i}.py", "content": "x"})
        assert result["action"] == "block"
        assert gate_state.get()["block_count"] == i + 1
