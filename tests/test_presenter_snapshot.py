"""Tests F5.4: snapshot del turno para el vestidor (mejora 2).

El vestidor deja de trabajar a ciegas: recibe un dict de HECHOS del
gate_state (archivos escritos en el turno, veredictos de tests/lint,
cierre limpio) y puede incorporarlos a la entrega. Dos invariantes:

1. Los hechos son del ESTADO (escritos por hooks reales), no texto del
   modelo: el vestidor no puede persuadir al snapshot.
2. Gate anti-invencion (nuevo): todo token factual del VESTIDO debe
   existir en el RAW o en los hechos del snapshot. Un vestidor que
   inventa paths/numeros que nadie produjo miente → fail-open al crudo.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

# Fixture arnes_plugin: definida en tests/conftest.py.


class _StubLlm:
    """Stub de la fachada PluginLlm: respuesta fijada + registro de llamadas."""

    def __init__(self, reply: str = "vestido", fail: bool = False):
        self.reply = reply
        self.fail = fail
        self.calls: List[Dict[str, Any]] = []

    def complete(self, messages, **kw):
        self.calls.append({"messages": messages, "kw": kw})
        if self.fail:
            raise RuntimeError("llm down (simulado)")
        return SimpleNamespace(text=self.reply)


class _EchoLlm:
    """Stub honesto: devuelve la persona + TODO el bloque factual que le
    pasaron (texto a vestir y contexto), conservando tokens."""

    def __init__(self, prefix: str = "VESTIDO:"):
        self.prefix = prefix
        self.calls: List[Dict[str, Any]] = []

    def complete(self, messages, **kw):
        self.calls.append({"messages": messages, "kw": kw})
        parts = []
        for m in messages:
            content = m.get("content", "") if isinstance(m, dict) else ""
            if "# Texto a vestir" in content:
                raw = content.split("# Texto a vestir", 1)[1].strip()
                parts.append(raw)
        return SimpleNamespace(text=f"{self.prefix} {' '.join(parts)}".strip())


def _reset_gate(arnes_plugin) -> Dict[str, Any]:
    arnes_plugin.gate_state.reset()
    return arnes_plugin.gate_state.get()


def _mk_soul(home: Path) -> None:
    (home / "SOUL.md").write_text(
        "# Identity\nSoy la voz del escenario.", encoding="utf-8")


# ----------------------------------------------------------------------------
# Snapshot: contenido (hechos del estado, no texto del modelo)
# ----------------------------------------------------------------------------

class TestTurnSnapshot:
    def test_snapshot_vacio_sin_actividad(self, arnes_plugin):
        _reset_gate(arnes_plugin)
        snap = arnes_plugin._presenter_turn_snapshot()
        assert snap == {}

    def test_snapshot_lleva_files_del_turno(self, arnes_plugin):
        _reset_gate(arnes_plugin)
        arnes_plugin._on_post_tool_call(
            tool_name="write_file",
            args={"path": "/repo/src/auth.py", "content": "x = 1"},
            result="ok")
        snap = arnes_plugin._presenter_turn_snapshot()
        assert "/repo/src/auth.py" in snap.get("files", [])

    def test_snapshot_se_resetea_por_turno(self, arnes_plugin):
        """Latch D7: turn_id nuevo vacia files del turno."""
        _reset_gate(arnes_plugin)
        arnes_plugin._on_post_tool_call(
            tool_name="write_file", args={"path": "/repo/a.py", "content": "1"},
            result="ok")
        arnes_plugin._on_pre_api_request(turn_id="t2")
        snap = arnes_plugin._presenter_turn_snapshot()
        assert snap.get("files", []) == []

    def test_snapshot_lleva_veredicto_tests(self, arnes_plugin):
        _reset_gate(arnes_plugin)
        gate = arnes_plugin.gate_state.get()
        gate["last_test_output"] = "12 passed in 0.5s"
        arnes_plugin._on_post_tool_call(
            tool_name="run_tests", args={}, result="OK: tests pasaron")
        snap = arnes_plugin._presenter_turn_snapshot()
        assert snap.get("tests") == "12 passed"

    def test_snapshot_veredicto_tests_rojo(self, arnes_plugin):
        _reset_gate(arnes_plugin)
        gate = arnes_plugin.gate_state.get()
        gate["last_test_output"] = "3 failed, 10 passed"
        arnes_plugin._on_post_tool_call(
            tool_name="run_tests", args={}, result="FALLO: rc=1")
        snap = arnes_plugin._presenter_turn_snapshot()
        assert "failed" in snap.get("tests", "")

    def test_snapshot_lleva_veredicto_lint(self, arnes_plugin):
        _reset_gate(arnes_plugin)
        gate = arnes_plugin.gate_state.get()
        gate["last_lint_output"] = "All checks passed!"
        arnes_plugin._on_post_tool_call(
            tool_name="run_lint", args={}, result="OK: linter limpio")
        snap = arnes_plugin._presenter_turn_snapshot()
        assert snap.get("lint") == "limpio"

    def test_snapshot_lleva_finish_clean(self, arnes_plugin):
        _reset_gate(arnes_plugin)
        gate = arnes_plugin.gate_state.get()
        gate["presenter_finish_clean"] = True
        snap = arnes_plugin._presenter_turn_snapshot()
        assert snap.get("finish_clean") is True


# ----------------------------------------------------------------------------
# El snapshot viaja al prompt del vestidor
# ----------------------------------------------------------------------------

class TestSnapshotEnPrompt:
    def _mk(self, arnes_plugin, tmp_path, monkeypatch, llm):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _mk_soul(tmp_path)
        return arnes_plugin.presenter.Presenter(llm)

    def test_bloque_contexto_en_prompt(self, arnes_plugin, tmp_path, monkeypatch):
        """El snapshot viaja como bloque '# Contexto del turno' ANTES del
        '# Texto a vestir' (compatibilidad del stub echo E2E)."""
        llm = _StubLlm(reply="La criatura hizo 3 tests")
        p = self._mk(arnes_plugin, tmp_path, monkeypatch, llm)
        p.present("hice cosas", ctx_snapshot={
            "files": ["/repo/auth.py"], "tests": "12 passed"})
        user_msg = str(llm.calls[0]["messages"])
        assert "# Contexto del turno" in user_msg
        assert "/repo/auth.py" in user_msg
        assert "12 passed" in user_msg
        # orden: contexto antes del texto a vestir
        assert user_msg.index("# Contexto del turno") < user_msg.index("# Texto a vestir")

    def test_sin_snapshot_no_hay_bloque(self, arnes_plugin, tmp_path, monkeypatch):
        llm = _StubLlm(reply="La criatura hizo cosas")
        p = self._mk(arnes_plugin, tmp_path, monkeypatch, llm)
        p.present("hice cosas")
        user_msg = str(llm.calls[0]["messages"])
        assert "# Contexto del turno" not in user_msg

    def test_prompt_permite_hechos_del_contexto(self, arnes_plugin, tmp_path, monkeypatch):
        """La regla del prompt autoriza explícitamente los hechos del contexto."""
        llm = _StubLlm(reply="La criatura trabajo en /repo/auth.py: 12 passed")
        p = self._mk(arnes_plugin, tmp_path, monkeypatch, llm)
        p.present(" listo ", ctx_snapshot={"files": ["/repo/auth.py"], "tests": "12 passed"})
        user_msg = str(llm.calls[0]["messages"])
        assert "contexto" in user_msg.lower()


# ----------------------------------------------------------------------------
# Gate anti-invencion: el vestido no puede inventar hechos
# ----------------------------------------------------------------------------

class TestAntiInvencion:
    def _mk(self, arnes_plugin, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _mk_soul(tmp_path)
        llm = _StubLlm()
        return arnes_plugin.presenter.Presenter(llm), llm

    def test_path_inventado_fail_open(self, arnes_plugin, tmp_path, monkeypatch):
        """El vestido menciona un path que no esta en el RAW ni en el
        snapshot: invento → crudo."""
        p, llm = self._mk(arnes_plugin, tmp_path, monkeypatch)
        llm.reply = "Toque /repo/imaginario.py y quedo perfecto"
        out = p.present("arregle el bug", ctx_snapshot={"files": ["/repo/real.py"]})
        assert out == "arregle el bug"

    def test_path_del_snapshot_autorizado(self, arnes_plugin, tmp_path, monkeypatch):
        """El vestido menciona el path del snapshot: hecho real, pasa."""
        p, llm = self._mk(arnes_plugin, tmp_path, monkeypatch)
        llm.reply = "La criatura forjo /repo/real.py: impecable"
        out = p.present("arregle el bug", ctx_snapshot={"files": ["/repo/real.py"]})
        assert out == "La criatura forjo /repo/real.py: impecable"

    def test_numero_del_snapshot_autorizado(self, arnes_plugin, tmp_path, monkeypatch):
        """'12' viene del veredicto de tests del snapshot: hecho real."""
        p, llm = self._mk(arnes_plugin, tmp_path, monkeypatch)
        llm.reply = "Doce pruebas: 12 passed, todo en orden"
        out = p.present("tests ok", ctx_snapshot={"tests": "12 passed"})
        assert "12 passed" in out

    def test_numero_inventado_fail_open(self, arnes_plugin, tmp_path, monkeypatch):
        """'47 tests' no esta en ningun lado: invento → crudo."""
        p, llm = self._mk(arnes_plugin, tmp_path, monkeypatch)
        llm.reply = "47 tests verdes, criatura"
        out = p.present("tests ok", ctx_snapshot={"tests": "12 passed"})
        assert out == "tests ok"

    def test_failopen_integrity_cuenta_como_invencion(self, arnes_plugin, tmp_path, monkeypatch):
        """Anti-invencion dispara el MISMO contador failopen:integrity (no
        uno nuevo): al usuario le basta saber que el vestido mintio."""
        p, llm = self._mk(arnes_plugin, tmp_path, monkeypatch)
        p2 = arnes_plugin.presenter.Presenter(
            llm, on_event=arnes_plugin._presenter_on_event)
        llm.reply = "Toque /repo/imaginario.py y quedo perfecto"
        p2.present("arregle el bug", ctx_snapshot={"files": ["/repo/real.py"]})
        gate = arnes_plugin.gate_state.get()
        assert gate["presenter_failopen_integrity"] >= 1

    def test_sin_snapshot_comportamiento_inalterado(self, arnes_plugin, tmp_path, monkeypatch):
        """Sin ctx_snapshot, el gate es el de siempre: tokens del RAW deben
        sobrevivir y no hay nada extra autorizado."""
        p, llm = self._mk(arnes_plugin, tmp_path, monkeypatch)
        llm.reply = "arregle el bug y tambien /otro/path.py"
        out = p.present("arregle el bug")
        assert out == "arregle el bug"  # /otro/path.py inventado → crudo


# ----------------------------------------------------------------------------
# Compatibilidad E2E: el stub echo sigue funcionando con contexto
# ----------------------------------------------------------------------------

class TestEchoCompat:
    def test_echo_stub_con_snapshot(self, arnes_plugin, tmp_path, monkeypatch):
        """El stub echo del E2E extrae tras '# Texto a vestir' — con el
        bloque de contexto ANTES, el marcador sigue al final y el echo
        conserva lo factual del texto a vestir."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _mk_soul(tmp_path)
        llm = _EchoLlm()
        p = arnes_plugin.presenter.Presenter(llm)
        out = p.present("done: 2 tests passed en /tmp/x.py",
                        ctx_snapshot={"files": ["/tmp/x.py"], "tests": "2 passed"})
        assert "2 tests passed" in out
        assert "/tmp/x.py" in out
