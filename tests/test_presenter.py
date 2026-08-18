"""Tests de Fase 5: el presenter (vestir output/preguntas sin contaminar al worker).

Diseno verificado 2026-08-18 contra el fork post-merge (3b26a67282):

- transform_llm_output es hook NATIVO del core: dispara una vez por turno en
  turn_finalizer DESPUES de persistir el mensaje assistant, asi que content
  queda RAW y solo la entrega al usuario se viste (Grieta 1 resuelta por
  estructura del core).
- Presenter recibe la fachada llm directamente (ctx.llm la construye el
  registro del plugin); los tests inyectan un stub plano.
- clarify se viste via directiva {"action": "modify"} de pre_tool_call:
  el seam triada aplica modified_args antes del dispatch.
- Routing: presenter_mode != "off" (default OFF, decision del usuario
  2026-08-18: deploy inerte) + worker_mode + platform != subagent +
  turno con tools (latch patron D7).

Lecciones D9/D11: todo hook nuevo exige test de invocacion directa; el
fixture arnes_plugin viene de tests/conftest.py (fuente unica, promovida
tras Q3/Q4) y se nombra como parametro, NUNCA se importa.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

# Fixture arnes_plugin: definida en tests/conftest.py.


class _StubLlm:
    """Stub de la fachada PluginLlm: respuesta fijada + registro de llamadas.

    Devuelve SimpleNamespace(text=...): el mirror minimo del unico campo
    que Presenter lee de PluginLlmCompleteResult.
    """

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
    """Estado limpio por test (leccion D9: el estado es global del modulo)."""
    arnes_plugin.gate_state.reset()
    return arnes_plugin.gate_state.get()


def _mk_soul(home: Path, worker_soul: str | None = None) -> None:
    """Escribe SOUL.md (o SOUL_WORKER.md si worker_soul) en HERMES_HOME."""
    if worker_soul is not None:
        (home / "SOUL_WORKER.md").write_text(worker_soul, encoding="utf-8")
    else:
        (home / "SOUL.md").write_text(
            "# Identity\nSoy la voz del escenario.", encoding="utf-8")


def _presenter_on(arnes_plugin, monkeypatch, tmp_path, llm=None,
                  mode="on", worker_mode=True, tool_calls=2) -> None:
    """Prende el circuito completo del presenter para un test.

    HERMES_HOME con SOUL, _PRESENTER_CTX inyectado y _worker_mode_active
    patcheado (config del core que el routing lee). El ctx fake es un
    SimpleNamespace(get_config=..., llm=...): el mismo contrato que el
    PluginContext real le exige al presenter.
    """
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
# presenter.Presenter: vestir con fail-open total
# ----------------------------------------------------------------------------

class TestPresenter:
    def test_present_viste_output(self, arnes_plugin, tmp_path, monkeypatch):
        """Output crudo pasa por LLM y vuelve vestido."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _mk_soul(tmp_path)
        llm = _StubLlm(reply="Bienvenidos a mi aquelarre: 3 tests verdes.")
        p = arnes_plugin.presenter.Presenter(llm)
        out = p.present("3 tests passed.")
        assert out == "Bienvenidos a mi aquelarre: 3 tests verdes."
        assert len(llm.calls) == 1
        prompt_text = llm.calls[0]["messages"]
        assert any("Soy la voz del escenario" in str(m) for m in prompt_text)
        assert any("3 tests passed" in str(m) for m in prompt_text)

    def test_present_sin_soul_devuelve_raw(self, arnes_plugin, tmp_path, monkeypatch):
        """Sin SOUL.md en HERMES_HOME no hay presenter: output original."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        llm = _StubLlm()
        p = arnes_plugin.presenter.Presenter(llm)
        assert p.present("raw output") == "raw output"
        assert llm.calls == []

    def test_present_llm_error_devuelve_raw(self, arnes_plugin, tmp_path, monkeypatch):
        """LLM caido: fail-open, output original, jamas excepcion."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _mk_soul(tmp_path)
        p = arnes_plugin.presenter.Presenter(_StubLlm(fail=True))
        assert p.present("raw output") == "raw output"

    def test_present_output_vacio_no_llama_llm(self, arnes_plugin, tmp_path, monkeypatch):
        """Output vacio: ni intenta, el silencio no se viste."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _mk_soul(tmp_path)
        llm = _StubLlm()
        p = arnes_plugin.presenter.Presenter(llm)
        assert p.present("") == ""
        assert p.present("   ") == "   "
        assert llm.calls == []

    def test_present_llm_resultado_vacio_devuelve_raw(self, arnes_plugin, tmp_path, monkeypatch):
        """LLM devuelve vacio: RAW, no entregar un turno en blanco por teatro."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _mk_soul(tmp_path)
        p = arnes_plugin.presenter.Presenter(_StubLlm(reply=""))
        assert p.present("raw output") == "raw output"

    def test_present_respeta_soul_worker_override(self, arnes_plugin, tmp_path, monkeypatch):
        """SOUL_WORKER.md presente: es EL soul del presenter (Task 5.4)."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "SOUL.md").write_text(
            "# Identity\nSoy la voz del escenario.", encoding="utf-8")
        _mk_soul(tmp_path, worker_soul="# Identity\nVoz tecnica del presenter.")
        llm = _StubLlm(reply="vestido")
        p = arnes_plugin.presenter.Presenter(llm)
        p.present("raw")
        prompt_text = llm.calls[0]["messages"]
        assert any("Voz tecnica del presenter" in str(m) for m in prompt_text)
        assert not any("Soy la voz del escenario" in str(m) for m in prompt_text)

    def test_dress_question_viste_pregunta(self, arnes_plugin, tmp_path, monkeypatch):
        """Pregunta seca vestida: el canal para clarify (Task 5.2)."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _mk_soul(tmp_path)
        llm = _StubLlm(reply="El camino se bifurca, criatura...")
        p = arnes_plugin.presenter.Presenter(llm)
        assert p.dress_question("Que branch base?") == "El camino se bifurca, criatura..."
        prompt_text = llm.calls[0]["messages"]
        assert any("pregunta" in str(m).lower() for m in prompt_text)

    def test_dress_question_fail_open(self, arnes_plugin, tmp_path, monkeypatch):
        """LLM caido durante clarify: pregunta original intacta."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _mk_soul(tmp_path)
        p = arnes_plugin.presenter.Presenter(_StubLlm(fail=True))
        assert p.dress_question("Deploy en prod?") == "Deploy en prod?"


# ----------------------------------------------------------------------------
# Routing: _on_transform_llm_output, las cinco condiciones
# ----------------------------------------------------------------------------

class TestRouting:
    def test_mode_off_no_transforma(self, arnes_plugin, tmp_path, monkeypatch):
        """(a) presenter_mode=off (default): jamas transforma."""
        _reset_gate(arnes_plugin)
        _presenter_on(arnes_plugin, monkeypatch, tmp_path, mode="off")
        try:
            result = arnes_plugin._on_transform_llm_output(
                response_text="raw", session_id="s", model="m",
                platform="telegram")
        finally:
            _presenter_off(arnes_plugin)
        assert result is None

    def test_subagent_no_transforma(self, arnes_plugin, tmp_path, monkeypatch):
        """(c) platform=subagent (hijos de delegate_task): None."""
        _reset_gate(arnes_plugin)
        _presenter_on(arnes_plugin, monkeypatch, tmp_path)
        try:
            result = arnes_plugin._on_transform_llm_output(
                response_text="raw", session_id="s", model="m",
                platform="subagent")
        finally:
            _presenter_off(arnes_plugin)
        assert result is None

    def test_worker_mode_off_no_transforma(self, arnes_plugin, tmp_path, monkeypatch):
        """(b) worker_mode apagado: el modelo YA es la persona, vestir es doble."""
        _reset_gate(arnes_plugin)
        _presenter_on(arnes_plugin, monkeypatch, tmp_path, worker_mode=False)
        try:
            result = arnes_plugin._on_transform_llm_output(
                response_text="raw", session_id="s", model="m",
                platform="telegram")
        finally:
            _presenter_off(arnes_plugin)
        assert result is None

    def test_turno_sin_tools_no_transforma(self, arnes_plugin, tmp_path, monkeypatch):
        """(d) charla pura (tools=0): solo se viste el cierre de tarea."""
        _reset_gate(arnes_plugin)
        _presenter_on(arnes_plugin, monkeypatch, tmp_path, tool_calls=0)
        try:
            result = arnes_plugin._on_transform_llm_output(
                response_text="raw", session_id="s", model="m",
                platform="telegram")
        finally:
            _presenter_off(arnes_plugin)
        assert result is None

    def test_sin_ctx_no_transforma(self, arnes_plugin, tmp_path, monkeypatch):
        """Sin _PRESENTER_CTX (plugin sin register real): None, sin tronar."""
        _reset_gate(arnes_plugin)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _mk_soul(tmp_path)
        arnes_plugin.gate_state.get()["presenter_tool_calls"] = 2
        monkeypatch.setattr(arnes_plugin, "_worker_mode_active", lambda: True)
        result = arnes_plugin._on_transform_llm_output(
            response_text="raw", session_id="s", model="m", platform="telegram")
        assert result is None

    def test_todo_on_transforma(self, arnes_plugin, tmp_path, monkeypatch):
        """Las cinco condiciones juntas: viste y devuelve el texto vestido."""
        _reset_gate(arnes_plugin)
        _presenter_on(arnes_plugin, monkeypatch, tmp_path,
                      llm=_StubLlm(reply="vestido ok"))
        try:
            result = arnes_plugin._on_transform_llm_output(
                response_text="raw", session_id="s", model="m",
                platform="telegram")
        finally:
            _presenter_off(arnes_plugin)
        assert result == "vestido ok"

    def test_todo_on_pero_sin_soul_no_transforma(self, arnes_plugin, tmp_path, monkeypatch):
        """(e) sin SOUL no hay persona que ponerse: None."""
        _reset_gate(arnes_plugin)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # sin SOUL.md
        arnes_plugin.gate_state.get()["presenter_tool_calls"] = 2
        arnes_plugin._PRESENTER_CTX = SimpleNamespace(
            get_config=lambda key, default=None: {"presenter_mode": "on"}.get(key, default),
            llm=_StubLlm(),
        )
        monkeypatch.setattr(arnes_plugin, "_worker_mode_active", lambda: True)
        try:
            result = arnes_plugin._on_transform_llm_output(
                response_text="raw", session_id="s", model="m",
                platform="telegram")
        finally:
            _presenter_off(arnes_plugin)
        assert result is None

    def test_vestido_identico_a_raw_devuelve_none(self, arnes_plugin, tmp_path, monkeypatch):
        """Vestidor devuelve el mismo texto: no marcar como transformado."""
        _reset_gate(arnes_plugin)
        _presenter_on(arnes_plugin, monkeypatch, tmp_path,
                      llm=_StubLlm(reply="raw"))
        try:
            result = arnes_plugin._on_transform_llm_output(
                response_text="raw", session_id="s", model="m",
                platform="telegram")
        finally:
            _presenter_off(arnes_plugin)
        assert result is None


# ----------------------------------------------------------------------------
# Latch patron D7: post_tool_call incrementa, pre_api_request resetea
# ----------------------------------------------------------------------------

class TestLatch:
    def test_post_tool_call_incrementa_latch(self, arnes_plugin):
        _reset_gate(arnes_plugin)
        gate = arnes_plugin.gate_state.get()
        assert gate["presenter_tool_calls"] == 0
        arnes_plugin._on_post_tool_call(
            tool_name="read_file", args={"path": "/x"}, result="ok")
        assert gate["presenter_tool_calls"] == 1
        arnes_plugin._on_post_tool_call(
            tool_name="terminal", args={"command": "ls"}, result="ok")
        assert gate["presenter_tool_calls"] == 2

    def test_pre_api_request_resetea_por_turno_nuevo(self, arnes_plugin):
        _reset_gate(arnes_plugin)
        gate = arnes_plugin.gate_state.get()
        gate["presenter_tool_calls"] = 4
        arnes_plugin._on_pre_api_request(turn_id="t1", user_message="hola")
        gate["presenter_tool_calls"] = 5
        arnes_plugin._on_pre_api_request(turn_id="t1", user_message="sigue")
        assert gate["presenter_tool_calls"] == 5  # mismo turno: no resetea
        arnes_plugin._on_pre_api_request(turn_id="t2", user_message="nuevo")
        assert gate["presenter_tool_calls"] == 0  # turno nuevo: resetea

    def test_transform_no_resetea_el_latch(self, arnes_plugin, tmp_path, monkeypatch):
        """Consumir el latch en transform no lo apaga: solo el turno nuevo."""
        _reset_gate(arnes_plugin)
        _presenter_on(arnes_plugin, monkeypatch, tmp_path, mode="off")
        gate = arnes_plugin.gate_state.get()
        try:
            result = arnes_plugin._on_transform_llm_output(
                response_text="raw", session_id="s", model="m",
                platform="telegram")
        finally:
            _presenter_off(arnes_plugin)
        assert result is None
        assert gate["presenter_tool_calls"] == 2  # intacto


# ----------------------------------------------------------------------------
# Clarify via modify directive (Task 5.2)
# ----------------------------------------------------------------------------

class TestClarify:
    def test_clarify_modify_viste_pregunta(self, arnes_plugin, tmp_path, monkeypatch):
        """pre_tool_call(clarify) devuelve modify con question vestida."""
        _reset_gate(arnes_plugin)
        _presenter_on(arnes_plugin, monkeypatch, tmp_path,
                      llm=_StubLlm(reply="Que camino, criatura?"), tool_calls=1)
        try:
            result = arnes_plugin._check_pre_tool_call(
                "clarify",
                {"question": "Que branch base?", "choices": ["develop", "main"]})
        finally:
            _presenter_off(arnes_plugin)
        assert result == {
            "action": "modify",
            "args": {"question": "Que camino, criatura?"},
        }

    def test_clarify_modify_fail_open(self, arnes_plugin, tmp_path, monkeypatch):
        """LLM caido: sin modify, la pregunta canonica pasa intacta."""
        _reset_gate(arnes_plugin)
        _presenter_on(arnes_plugin, monkeypatch, tmp_path,
                      llm=_StubLlm(fail=True), tool_calls=1)
        try:
            result = arnes_plugin._check_pre_tool_call(
                "clarify",
                {"question": "Que branch base?", "choices": ["develop", "main"]})
        finally:
            _presenter_off(arnes_plugin)
        assert result is None

    def test_clarify_off_no_toca(self, arnes_plugin, tmp_path, monkeypatch):
        """presenter_mode=off: clarify pasa sin modify (deploy inerte)."""
        _reset_gate(arnes_plugin)
        _presenter_on(arnes_plugin, monkeypatch, tmp_path,
                      llm=_StubLlm(reply="no debe usarse"), mode="off",
                      tool_calls=1)
        try:
            result = arnes_plugin._check_pre_tool_call(
                "clarify", {"question": "Que branch base?"})
        finally:
            _presenter_off(arnes_plugin)
        assert result is None

    def test_clarify_end_to_end_canonico(self, arnes_plugin, tmp_path, monkeypatch):
        """Circuito completo: modify viste, callback ve vestida, respuesta limpia."""
        _reset_gate(arnes_plugin)
        _presenter_on(arnes_plugin, monkeypatch, tmp_path,
                      llm=_StubLlm(reply="El camino se bifurca, criatura..."),
                      tool_calls=1)
        try:
            result = arnes_plugin._check_pre_tool_call(
                "clarify",
                {"question": "Que branch base?", "choices": ["develop", "main"]})
        finally:
            _presenter_off(arnes_plugin)
        assert result and result["action"] == "modify"
        dressed = result["args"]["question"]

        # Simulamos la plataforma: el usuario ve la vestida y elige develop.
        seen: Dict[str, Any] = {}

        def platform_callback(question, choices, multi_select=False):
            seen["question"] = question
            seen["choices"] = choices
            return "develop"

        from tools.clarify_tool import clarify_tool
        raw = clarify_tool(
            question=dressed, choices=["develop", "main"],
            multi_select=False, callback=platform_callback)
        payload = json.loads(raw)
        # El worker recibe eleccion canonica, no presentacion.
        assert payload["user_response"] == "develop"
        # Y la plataforma vio la pregunta vestida.
        assert seen["question"] == dressed
        assert seen["choices"][0] == "develop (Recommended)"


# ----------------------------------------------------------------------------
# Registro: el wiring existe y es legible
# ----------------------------------------------------------------------------

class TestRegistro:
    def test_register_registra_transform_llm_output(self, arnes_plugin):
        """register() cuelga el hook nuevo en el PluginContext."""
        registered: Dict[str, Any] = {}

        class _Ctx:
            def register_hook(self, name, cb):
                registered[name] = cb

            def register_tool(self, **kw):
                pass

        try:
            arnes_plugin.register(_Ctx())
        finally:
            _presenter_off(arnes_plugin)
        assert "transform_llm_output" in registered

    def test_state_tiene_keys_presenter(self, arnes_plugin):
        _reset_gate(arnes_plugin)
        s = arnes_plugin.gate_state.get()
        assert "presenter_tool_calls" in s
        assert "presenter_turn_id" in s
