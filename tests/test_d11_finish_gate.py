"""Tests de D11: el deadlock finish/commit gate en sesiones no-coding.

Caso real (2026-08-18): una sesión del agente con cwd /opt/hermes (fuera de
todo repo git) edita el plugin dentro del fork. El core resuelve
coding=False (is_coding_context: cwd fuera de workspace de código). El finish
gate (_on_pre_verify) tenía un early-return `if not coding or not
changed_paths: return None` — nunca arma, `done` jamás se prende, y el commit
gate (Q5) exige done=True → bloqueo PERMANENTE sin evidencia que lo limpie.

Diseño del fix (tallado en la bóveda):
  1. _on_pre_verify: con coding=False, NO early-return ciego. Si hay
     changed_paths, seguir el mismo ritual (scope si fuera-de-repo + evidencia
     tests/lint/typecheck) y prender done=True al pasar.
  2. Commit gate: si done apagado, aceptar evidencia fresca del state
     (scope_done + tests pass + lint pass) como alternativa — misma sangre,
     otra puerta.

Estos tests ejercitan _on_pre_verify y el commit gate directamente (lección
D9: todo hook nuevo o cambiado exige test de invocación directa).
"""
from __future__ import annotations

from pathlib import Path

# Fixture arnes_plugin: definida en tests/conftest.py (fuente única, promovida
# tras el reclamo Q3/Q4 de duplicación) — pytest la descubre sola, sin import.

_REPO_FILE = str(Path(__file__).resolve().parent.parent / "pyproject.toml")


def _reset_gate(gate):
    """Estado limpio por test: done apagado, evidencia vacía."""
    gate["done"] = False
    gate["scope_done"] = False
    gate["last_test_output"] = None
    gate["last_lint_output"] = None
    gate["last_typecheck_output"] = None


def _evidencia_fresca(gate, test_out="4 passed in 0.5s", lint_out="All checks passed!"):
    """Evidencia del ritual: tests + lint verdes (como las tools del arnés)."""
    gate["last_test_output"] = test_out
    gate["last_lint_output"] = lint_out


class TestFinishGateNonCoding:
    """Puerta A: _on_pre_verify con coding=False."""

    def test_no_coding_con_cambios_en_repo_arma_done(self, arnes_plugin):
        """El caso D11 exacto: coding=False, cambios dentro de un repo git.

        El finish gate debe evaluar evidencia igual que en coding=True y
        prender done=True cuando todo pasa — no early-return que deja done
        muerto para siempre.
        """
        gate = arnes_plugin.gate_state.get()
        _reset_gate(gate)
        _evidencia_fresca(gate)

        result = arnes_plugin._on_pre_verify(coding=False, changed_paths=[_REPO_FILE])

        assert result is None, f"debió dejar cerrar: {result}"
        assert gate["done"] is True

    def test_no_coding_sin_scope_y_cambio_fuera_de_repo_bloquea(self, arnes_plugin, tmp_path):
        """Guard D6 intacto: fuera de todo repo, coding=False exige scope."""
        gate = arnes_plugin.gate_state.get()
        _reset_gate(gate)
        _evidencia_fresca(gate)
        outside = tmp_path / "loose.md"
        outside.write_text("x")

        result = arnes_plugin._on_pre_verify(coding=False, changed_paths=[str(outside)])

        assert result is not None
        assert result["action"] == "continue"
        assert "scope" in result["message"].lower()
        assert gate["done"] is False

    def test_no_coding_con_scope_y_cambio_fuera_de_repo_arma_done(self, arnes_plugin, tmp_path):
        """Fuera de repo PERO con scope aprobado + evidencia: arma done."""
        gate = arnes_plugin.gate_state.get()
        _reset_gate(gate)
        gate["scope_done"] = True
        _evidencia_fresca(gate)
        outside = tmp_path / "loose.md"
        outside.write_text("x")

        result = arnes_plugin._on_pre_verify(coding=False, changed_paths=[str(outside)])

        assert result is None
        assert gate["done"] is True

    def test_no_coding_tests_rojos_bloquean_igual(self, arnes_plugin):
        """El ritual no se relaja: tests rojos bloquean aunque coding=False."""
        gate = arnes_plugin.gate_state.get()
        _reset_gate(gate)
        _evidencia_fresca(gate, test_out="2 passed, 3 failed in 1.0s")

        result = arnes_plugin._on_pre_verify(coding=False, changed_paths=[_REPO_FILE])

        assert result is not None
        assert result["action"] == "continue"
        assert "BLOQUEADO" in result["message"]
        assert gate["done"] is False


class TestCommitGateFreshEvidence:
    """Puerta B: commit gate acepta evidencia fresca cuando done apagado."""

    def _commit(self, plugin):
        return plugin._on_pre_tool_call(
            "terminal", {"command": "git commit -F /workspace/commit-msg-d11.txt"}
        )

    def test_commit_con_done_apagado_y_evidencia_fresca_pasa(self, arnes_plugin):
        """done=False pero scope+tests+lint frescos: el commit pasa.

        Cubre la ventana del turno siguiente (Puerta B del deadlock): los
        edits fueron en el turno anterior, el finish gate nunca corrió en
        este, pero la evidencia del state sigue siendo la del ritual.
        """
        gate = arnes_plugin.gate_state.get()
        _reset_gate(gate)
        gate["scope_done"] = True
        _evidencia_fresca(gate)
        gate["done"] = False

        result = self._commit(arnes_plugin)

        assert result is None, f"debía permitir el commit: {result}"

    def test_commit_con_done_apagado_y_tests_rojos_bloquea(self, arnes_plugin):
        """Evidencia roja no abre la puerta: ritual intacto."""
        gate = arnes_plugin.gate_state.get()
        _reset_gate(gate)
        gate["scope_done"] = True
        _evidencia_fresca(gate, test_out="1 passed, 2 failed in 1.0s")
        gate["done"] = False

        result = self._commit(arnes_plugin)

        assert result is not None
        assert result["action"] == "block"
        assert "BLOQUEADO" in result["message"]

    def test_commit_con_done_apagado_sin_evidencia_bloquea(self, arnes_plugin):
        """Sin evidencia ni done: el bloqueo legítimo se mantiene."""
        gate = arnes_plugin.gate_state.get()
        _reset_gate(gate)

        result = self._commit(arnes_plugin)

        assert result is not None
        assert result["action"] == "block"
        assert "BLOQUEADO" in result["message"]
