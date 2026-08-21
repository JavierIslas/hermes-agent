"""Tests F5.3: presenter_mode on_finish + telemetria de fail-open + fix contrato model.

Tres mejoras del par worker/presenter (2026-08-21):

1. ``presenter_mode: on_finish`` — viste SOLO el cierre de la tarea, no cada
   turno con tools. Senal: latch por turno ``presenter_finish_clean``, armando
   en pre_verify exitoso (el finish gate dejo cerrar con changed_paths) o en
   commit ejecutado; reset por turn_id nuevo (patron D7).
2. Telemetria de fail-open: el Presenter reporta via callback ``on_event``
   (``dressed`` / ``failopen:integrity|llm|timeout|empty|nosoul``); el hook
   acumula contadores en gate_state y ``on_session_end`` loguea el resumen.
   Dogfood D13: 2 de 3 vestidos morian en timeout y nadie lo sabia.
3. Fix contrato ``_model_kw``: la firma real de PluginLlm.complete() es
   ``provider=`` y ``model=`` SEPARADOS (verificado /opt/hermes/agent/plugin_llm.py:740,
   gateado por plugins.entries.<id>.llm.allow_model_override). El codigo
   viejo pasaba "provider/model" como un solo string ``model`` →
   PluginLlmTrustError → fail-open silencioso SIEMPRE.
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


def _reset_gate(arnes_plugin) -> Dict[str, Any]:
    arnes_plugin.gate_state.reset()
    return arnes_plugin.gate_state.get()


def _mk_soul(home: Path, worker_soul: str | None = None) -> None:
    if worker_soul is not None:
        (home / "SOUL_WORKER.md").write_text(worker_soul, encoding="utf-8")
    else:
        (home / "SOUL.md").write_text(
            "# Identity\nSoy la voz del escenario.", encoding="utf-8")


def _presenter_on(arnes_plugin, monkeypatch, tmp_path, llm=None,
                  mode="on", worker_mode=True, tool_calls=2) -> None:
    """Prende el circuito completo del presenter para un test."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _mk_soul(tmp_path)
    gate = arnes_plugin.gate_state.get()
    gate["presenter_tool_calls"] = tool_calls
    settings: Dict[str, Any] = {"presenter_mode": mode}
    arnes_plugin._PRESENTER_CTX = SimpleNamespace(
        get_config=lambda key, default=None: settings.get(key, default),
        llm=llm or _StubLlm(),
    )
    monkeypatch.setattr(arnes_plugin, "_worker_mode_active", lambda: worker_mode)


def _presenter_off(arnes_plugin) -> None:
    arnes_plugin._PRESENTER_CTX = None


# ----------------------------------------------------------------------------
# (1) presenter_mode: on_finish — routing por cierre de tarea
# ----------------------------------------------------------------------------

class TestModeOnFinish:
    """``on_finish``: el vestidor solo corre en el turno que CIERRA la tarea."""

    def _run(self, arnes_plugin, monkeypatch, tmp_path, mode: Any = "on_finish",
             finish_clean=False, tool_calls=2, reply="vestido ok"):
        _reset_gate(arnes_plugin)
        _presenter_on(arnes_plugin, monkeypatch, tmp_path, mode=mode,
                      llm=_StubLlm(reply=reply), tool_calls=tool_calls)
        gate = arnes_plugin.gate_state.get()
        gate["presenter_finish_clean"] = finish_clean
        try:
            return arnes_plugin._on_transform_llm_output(
                response_text="raw", session_id="s", model="m",
                platform="telegram")
        finally:
            _presenter_off(arnes_plugin)

    def test_on_finish_sin_cierre_no_viste(self, arnes_plugin, tmp_path, monkeypatch):
        """Turno intermedio (hubo tools pero no cerro la tarea): None."""
        result = self._run(arnes_plugin, monkeypatch, tmp_path,
                           finish_clean=False)
        assert result is None

    def test_on_finish_con_cierre_viste(self, arnes_plugin, tmp_path, monkeypatch):
        """Turno con finish_clean (finish gate dejo cerrar): viste."""
        result = self._run(arnes_plugin, monkeypatch, tmp_path,
                           finish_clean=True)
        assert result == "vestido ok"

    def test_on_finish_bool_true_es_on_retrocompat(self, arnes_plugin, tmp_path, monkeypatch):
        """Bool true (YAML natural) sigue significando on (cada turno con tools):
        retrocompatible con el dogfood 2026-08-18."""
        result = self._run(arnes_plugin, monkeypatch, tmp_path, mode=True,
                           finish_clean=False)
        assert result == "vestido ok"

    def test_on_finish_off_no_viste_nunca(self, arnes_plugin, tmp_path, monkeypatch):
        """off sigue siendo off aunque haya finish_clean."""
        result = self._run(arnes_plugin, monkeypatch, tmp_path, mode="off",
                           finish_clean=True)
        assert result is None

    def test_latch_finish_clean_se_resetea_por_turno(self, arnes_plugin):
        """Latch D7: turn_id nuevo en pre_api_request resetea finish_clean."""
        _reset_gate(arnes_plugin)
        gate = arnes_plugin.gate_state.get()
        gate["presenter_finish_clean"] = True
        arnes_plugin._on_pre_api_request(turn_id="t2")
        assert gate["presenter_finish_clean"] is False

    def test_latch_finish_clean_armado_por_pre_verify(self, arnes_plugin):
        """pre_verify exitoso con changed_paths arma finish_clean."""
        _reset_gate(arnes_plugin)
        gate = arnes_plugin.gate_state.get()
        gate["scope_done"] = True  # camino limpio: scope ya aprobado
        arnes_plugin._on_pre_verify(
            changed_paths=["/x/y.py"], coding=True,
            session_id="s", platform="cli")
        gate = arnes_plugin.gate_state.get()
        assert gate["presenter_finish_clean"] is True

    def test_latch_no_se_arma_sin_changed_paths(self, arnes_plugin):
        """Sin changed_paths el pre_verify devuelve None inmediato: no es cierre."""
        _reset_gate(arnes_plugin)
        arnes_plugin._on_pre_verify(
            changed_paths=[], coding=True, session_id="s", platform="cli")
        gate = arnes_plugin.gate_state.get()
        assert gate["presenter_finish_clean"] is not True

    def test_latch_no_se_arma_cuando_el_gate_bloquea(self, arnes_plugin):
        """Turno con escrituras fuera de repo sin scope: el finish gate devuelve
        {'action': 'continue'} — no es un cierre limpio, no arma el latch."""
        _reset_gate(arnes_plugin)
        gate = arnes_plugin.gate_state.get()
        gate["scope_done"] = False
        result = arnes_plugin._on_pre_verify(
            changed_paths=["/fuera/de/repo/x.py"], coding=True,
            session_id="s", platform="cli")
        assert result is not None  # el gate bloqueo
        assert gate["presenter_finish_clean"] is not True

    def test_latch_armado_por_commit_exitoso(self, arnes_plugin):
        """Commit ejecutado con exito (post-finish o evidencia fresca): arma."""
        _reset_gate(arnes_plugin)
        arnes_plugin._on_post_tool_call(
            tool_name="terminal",
            args={"command": "git commit -F /tmp/msg.txt"},
            result="ok", status="ok")
        gate = arnes_plugin.gate_state.get()
        assert gate["presenter_finish_clean"] is True

    def test_latch_no_se_arma_por_commit_bloqueado(self, arnes_plugin):
        """Commit bloqueado por el commit gate (status=error): no arma."""
        _reset_gate(arnes_plugin)
        arnes_plugin._on_post_tool_call(
            tool_name="terminal",
            args={"command": "git commit -m 'x'"},
            result="blocked", status="error")
        gate = arnes_plugin.gate_state.get()
        assert gate["presenter_finish_clean"] is not True


# ----------------------------------------------------------------------------
# (2) Telemetria de fail-open del vestidor
# ----------------------------------------------------------------------------

class TestFailOpenTelemetry:
    """El vestidor reporta dressed/failopen via on_event; el hook acumula."""

    def _mk(self, arnes_plugin, tmp_path, monkeypatch, reply="vestido",
            fail=False, raw="3 tests passed en /tmp/x.py"):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _mk_soul(tmp_path)
        llm = _StubLlm(reply=reply, fail=fail)
        p = arnes_plugin.presenter.Presenter(
            llm, on_event=arnes_plugin._presenter_on_event)
        return p, llm

    def test_dressed_incrementa_contador(self, arnes_plugin, tmp_path, monkeypatch):
        _reset_gate(arnes_plugin)
        p, llm = self._mk(arnes_plugin, tmp_path, monkeypatch,
                          reply="Miren: 3 tests en /tmp/x.py")
        out = p.present("3 tests passed en /tmp/x.py")
        assert out != "3 tests passed en /tmp/x.py"
        gate = arnes_plugin.gate_state.get()
        assert gate["presenter_dressed_count"] >= 1

    def test_failopen_integridad_cuenta(self, arnes_plugin, tmp_path, monkeypatch):
        """Vestido que pierde un token factual → fail-open + contador."""
        _reset_gate(arnes_plugin)
        p, llm = self._mk(arnes_plugin, tmp_path, monkeypatch,
                          reply="todo bien, sin datos")  # pierde /tmp/x.py y 3
        out = p.present("3 tests passed en /tmp/x.py")
        assert out == "3 tests passed en /tmp/x.py"  # crudo
        gate = arnes_plugin.gate_state.get()
        assert gate["presenter_failopen_integrity"] >= 1

    def test_failopen_llm_cuenta(self, arnes_plugin, tmp_path, monkeypatch):
        """LLM caido → fail-open + contador."""
        _reset_gate(arnes_plugin)
        p, llm = self._mk(arnes_plugin, tmp_path, monkeypatch, fail=True)
        out = p.present("3 tests passed en /tmp/x.py")
        assert out == "3 tests passed en /tmp/x.py"
        gate = arnes_plugin.gate_state.get()
        assert gate["presenter_failopen_llm"] >= 1

    def test_failopen_timeout_cuenta(self, arnes_plugin, tmp_path, monkeypatch):
        """Timeout de la llamada (raise dentro de present) → contador propio.

        Distinguir timeout de otro error de LLM importa: D13 mostro que el
        timeout es la causa dominante (outputs largos con modelo host lento).
        """
        _reset_gate(arnes_plugin)

        class _TimeoutLlm(_StubLlm):
            def complete(self, messages, **kw):
                self.calls.append({"messages": messages, "kw": kw})
                raise TimeoutError("dial timeout")

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _mk_soul(tmp_path)
        p = arnes_plugin.presenter.Presenter(
            _TimeoutLlm(), on_event=arnes_plugin._presenter_on_event)
        out = p.present("3 tests passed en /tmp/x.py")
        assert out == "3 tests passed en /tmp/x.py"
        gate = arnes_plugin.gate_state.get()
        assert gate["presenter_failopen_timeout"] >= 1

    def test_failopen_empty_cuenta(self, arnes_plugin, tmp_path, monkeypatch):
        """Respuesta vestida vacia → contador propio."""
        _reset_gate(arnes_plugin)
        p, llm = self._mk(arnes_plugin, tmp_path, monkeypatch, reply="   ")
        out = p.present("3 tests passed en /tmp/x.py")
        assert out == "3 tests passed en /tmp/x.py"
        gate = arnes_plugin.gate_state.get()
        assert gate["presenter_failopen_empty"] >= 1

    def test_contadores_existen_en_state(self, arnes_plugin):
        _reset_gate(arnes_plugin)
        s = arnes_plugin.gate_state.get()
        assert "presenter_failopen_integrity" in s
        assert "presenter_failopen_llm" in s
        assert "presenter_failopen_timeout" in s
        assert "presenter_failopen_empty" in s


# ----------------------------------------------------------------------------
# (3) Fix contrato _model_kw: provider= y model= separados
# ----------------------------------------------------------------------------

class TestModelKwContract:
    """La firma real de PluginLlm.complete() toma provider= y model= aparte."""

    def _mk(self, arnes_plugin, tmp_path, monkeypatch, settings):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _mk_soul(tmp_path)
        llm = _StubLlm(reply="vestido del flash")
        p = arnes_plugin.presenter.Presenter(
            llm, get_config=lambda key, default=None: settings.get(key, default))
        return p, llm

    def test_ref_completa_se_divide_en_provider_y_model(self, arnes_plugin, tmp_path, monkeypatch):
        """"zai/glm-4.5-flash" → provider="zai", model="glm-4.5-flash"."""
        p, llm = self._mk(arnes_plugin, tmp_path, monkeypatch,
                          {"presenter_model": "zai/glm-4.5-flash"})
        p.present("raw con path /tmp/x.py")
        kw = llm.calls[0]["kw"]
        assert kw.get("provider") == "zai"
        assert kw.get("model") == "glm-4.5-flash"

    def test_model_solo_va_como_model(self, arnes_plugin, tmp_path, monkeypatch):
        """Sin barra: todo es el model, provider queda al host (None no se pasa)."""
        p, llm = self._mk(arnes_plugin, tmp_path, monkeypatch,
                          {"presenter_model": None,
                           "presenter_model": "glm-4.5-flash"})
        p.present("raw con path /tmp/x.py")
        kw = llm.calls[0]["kw"]
        assert kw.get("model") == "glm-4.5-flash"
        assert "provider" not in kw

    def test_bool_true_se_ignora(self, arnes_plugin, tmp_path, monkeypatch):
        """Bool True de YAML natural NO es ref de modelo: nada se pasa."""
        p, llm = self._mk(arnes_plugin, tmp_path, monkeypatch,
                          {"presenter_model": True})
        p.present("raw con path /tmp/x.py")
        kw = llm.calls[0]["kw"]
        assert "model" not in kw
        assert "provider" not in kw

    def test_ref_con_demas_barras_es_todo_model(self, arnes_plugin, tmp_path, monkeypatch):
        """"openrouter/qwen/qwen-2.5": primera barra divide, resto es model."""
        p, llm = self._mk(arnes_plugin, tmp_path, monkeypatch,
                          {"presenter_model": "openrouter/qwen/qwen-2.5"})
        p.present("raw con path /tmp/x.py")
        kw = llm.calls[0]["kw"]
        assert kw.get("provider") == "openrouter"
        assert kw.get("model") == "qwen/qwen-2.5"

    def test_default_no_pasa_model(self, arnes_plugin, tmp_path, monkeypatch):
        """Sin setting: NO se pasa model= → la fachada usa el modelo host."""
        p, llm = self._mk(arnes_plugin, tmp_path, monkeypatch, {})
        p.present("raw con path /tmp/x.py")
        assert "model" not in llm.calls[0]["kw"]


# ----------------------------------------------------------------------------
# (4) Rift 3: directiva de mensajes con persona en el prompt del worker
# ----------------------------------------------------------------------------

def _worker_prompt_mod(arnes_plugin):
    """Importa el canon del worker prompt (submodulo del plugin)."""
    import importlib
    return importlib.import_module("hermes_plugins.arnes_gates.worker_prompt")


class TestWorkerRift3Directive:
    """El WORKER_PROMPT canon ensena a procesar mensajes libres del usuario:
    extraer el intento tecnico, ignorar el tono/persona (decision Rift 3,
    sesion 2026-08-10: sin capa de normalizacion LLM)."""

    def test_directiva_presente_en_canon(self, arnes_plugin):
        wp = _worker_prompt_mod(arnes_plugin).WORKER_PROMPT
        assert "persona" in wp.lower()
        assert "intent" in wp.lower() or "intento" in wp.lower()

    def test_directiva_menciona_tono_y_clarify(self, arnes_plugin):
        wp = _worker_prompt_mod(arnes_plugin).WORKER_PROMPT
        low = wp.lower()
        assert "tono" in low
        assert "clarify" in low
