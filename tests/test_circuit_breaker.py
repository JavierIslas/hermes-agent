"""Tests del circuit breaker: detección de loops de bloqueos del arnés."""
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
    """Una vez tripped, las tools mutativas se bloquean — pero Fix D7: la
    lectura y analyze_scope quedan exentas (eran el deadlock: la salida
    prescripta del propio breaker era inalcanzable)."""
    from hermes_plugins.arnes_gates import _on_pre_tool_call, gate_state

    # Trippear el circuit breaker.
    gate_state.get()["circuit_tripped"] = True

    # write_file (mutativa): bloqueado.
    result = _on_pre_tool_call(tool_name="write_file", args={"path": "/tmp/x.py", "content": "x"})
    assert result is not None
    assert result["action"] == "block"
    assert "CIRCUIT BREAKER" in result["message"]

    # read_file: eximido — el agente puede diagnosticar.
    result = _on_pre_tool_call(tool_name="read_file", args={"path": "/tmp/foo.py"})
    assert result is None


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


# =============================================================================
# Fix D7: exenciones + rearme humano (dogfooding 2026-08-15).
# =============================================================================
def test_exempt_tools_pass_when_tripped():
    """D7: analyze_scope y lectura pasan aunque el breaker esté tripped.

    Sin esto, la salida prescripta del propio breaker ("llama analyze_scope")
    es inalcanzable desde dentro del turno: deadlock.
    """
    from hermes_plugins.arnes_gates import _on_pre_tool_call, gate_state

    gate_state.get()["circuit_tripped"] = True

    # analyze_scope sigue respondiendo: el agente puede pedir scope nuevo.
    result = _on_pre_tool_call(tool_name="analyze_scope", args={})
    assert result is None

    # Lectura sigue pasando: el agente puede diagnosticar.
    result = _on_pre_tool_call(tool_name="read_file", args={"path": "/tmp/foo.py"})
    assert result is None


def test_non_exempt_still_blocked_when_tripped():
    """D7: las tools mutativas siguen bloqueadas con el breaker tripped."""
    from hermes_plugins.arnes_gates import _on_pre_tool_call, gate_state

    gate_state.get()["circuit_tripped"] = True

    result = _on_pre_tool_call(tool_name="write_file", args={"path": "/tmp/x.py", "content": "x"})
    assert result is not None
    assert result["action"] == "block"
    assert "CIRCUIT BREAKER" in result["message"]


def test_rearm_on_new_human_turn():
    """D7: un turno humano nuevo (turn_id distinto) rearma el breaker.

    El usuario ya interrumpió el loop al escribir: insistir con el breaker
    solo secuestra su turno.
    """
    from hermes_plugins.arnes_gates import _on_pre_api_request, gate_state

    gate_state.get()["circuit_tripped"] = True
    gate_state.get()["block_count"] = 7

    _on_pre_api_request(turn_id="turno-2", user_message="ya lo autorizo")

    assert gate_state.get()["circuit_tripped"] is False
    assert gate_state.get()["block_count"] == 0


def test_no_rearm_within_same_turn():
    """D7: reintentos dentro del MISMO turno no rearman el breaker."""
    from hermes_plugins.arnes_gates import _on_pre_api_request, gate_state

    # Flujo real: el turno arranca con su pre_api_request (registra turn_id).
    _on_pre_api_request(turn_id="turno-1", user_message="hola")

    # El agente se bloquea 5 veces a mitad del turno → breaker tripped.
    gate_state.get()["circuit_tripped"] = True
    gate_state.get()["block_count"] = 9

    # Más llamadas LLM dentro del mismo turno (continuación/retry): no rearme.
    _on_pre_api_request(turn_id="turno-1", user_message="hola")
    assert gate_state.get()["circuit_tripped"] is True

    _on_pre_api_request(turn_id="turno-1", user_message="hola")
    assert gate_state.get()["circuit_tripped"] is True


# =============================================================================
# Fix D5: el bloqueo del breaker debe convertirse en halt real del loop.
# =============================================================================
def test_breaker_block_carries_halt_loop_flag():
    """D5: cuando el breaker trippea, el directive block lleva halt_loop=True
    para que el dispatcher lo convierta en ToolGuardrailDecision(halt)."""
    from hermes_plugins.arnes_gates import _on_pre_tool_call, gate_state

    gate_state.reset()
    # Tripear el breaker con 5 bloqueos consecutivos.
    for _ in range(5):
        result = _on_pre_tool_call(
            "write_file", {"path": "/tmp/fuera/x.py", "content": "x"}
        )
    assert gate_state.get()["circuit_tripped"] is True

    # El 6º intento (ya tripped) debe llevar el flag.
    result = _on_pre_tool_call(
        "write_file", {"path": "/tmp/fuera/y.py", "content": "y"}
    )
    assert result is not None
    assert result["action"] == "block"
    assert result.get("halt_loop") is True


def test_normal_block_does_not_carry_halt_loop():
    """D5: un bloqueo normal (no-breaker) NO lleva halt_loop — solo el
    breaker pide el corte del loop."""
    from hermes_plugins.arnes_gates import _on_pre_tool_call, gate_state

    gate_state.reset()
    result = _on_pre_tool_call(
        "write_file", {"path": "/tmp/fuera/z.py", "content": "z"}
    )
    assert result is not None
    cost = result.get("action") == "block"
    assert cost is True
    assert "halt_loop" not in result


def test_exempt_tool_while_tripped_does_not_halt():
    """D7+D5: las exentas (lectura/analyze_scope) ni se bloquean ni piden
    halt — son la puerta de escape del deadlock."""
    from hermes_plugins.arnes_gates import _on_pre_tool_call, gate_state

    gate_state.reset()
    for _ in range(5):
        _on_pre_tool_call("write_file", {"path": "/tmp/fuera/x.py", "content": "x"})
    assert gate_state.get()["circuit_tripped"] is True

    # read_file estando tripped: pasa (D7), sin halt_loop.
    result = _on_pre_tool_call("read_file", {"path": "/tmp/fuera/x.py"})
    assert result is None
