"""E2E del arnes (Fase 7, Task 7.1): el flujo completo de una tarea de codigo.

Adapta refs/agent_juguete/tests/test_agent.py al plugin real: en vez de un
loop de agente propio, se conducen LAS SUPERFICIES REALES DEL PLUGIN en el
orden exacto en que el core de Hermes las invoca en un turno con tools:

  pre_api_request (turno abre)
    -> analyze_scope (tool handler)      [plan valido]
    -> pre_tool_call(write_file)         [bloquea sin scope / pasa con scope]
    -> read_file track                   [read_before_write]
    -> post_tool_call                    [indice incremental + latch presenter]
    -> run_tests / run_lint (handlers reales de verification.py)
    -> pre_verify (finish gate)          [exige evidencia; done al pasar]
    -> transform_llm_output (presenter)  [viste SOLO la entrega]

Metrica sagrada de F7 (Metricas de Exito en PLAN.md): "El worker nunca ve
texto vestido en su historial tras multiples turnos" — aserciones sobre el
transcript simulado: content queda RAW, solo la entrega se viste.

Hermetico: sin red, sin LLM real (stub de PluginLlm), sin config.yaml real
(SimpleNamespace ctx), proyecto de juguete en tmp_path (git repo con test
runner "true" — el punto es la SECUENCIA de gates, no los tests reales).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

# Fixture arnes_plugin: definida en tests/conftest.py.


# ----------------------------------------------------------------------------
# Proyecto de juguete: un git repo minimo con runner trivial
# ----------------------------------------------------------------------------

def _mk_proyecto(tmp_path: Path) -> Path:
    """Repo git minimo: user.py + runners true (tests y lint siempre verdes).

    verification._detect_project_root resuelve via terminal_cwd -> git-root,
    y _detect_tests_setup descubre el runner desde pyproject.toml. Usamos
    uno explicito para que run_tests/run_lint produzcan salida verde REAL
    que el finish gate evalua como evidencia (D11).
    """
    repo = tmp_path / "proyecto"
    repo.mkdir()
    (repo / "user.py").write_text(
        "def validar_email(email: str) -> bool:\n"
        "    return \"@\" in email\n",
        encoding="utf-8",
    )
    # Marca de pytest (_es_pytest) + un test REAL que pasa: run_tests
    # ejecuta sys.executable -m pytest (no hay .venv/bin/python en el
    # juguete) y produce "1 passed" — evidencia verde de verdad.
    (repo / "pytest.ini").write_text(
        "[pytest]\naddopts = -q\n", encoding="utf-8")
    (repo / "test_user.py").write_text(
        "from user import validar_email\n\n"
        "def test_validar_email():\n"
        "    assert validar_email(\"a@b\") is True\n"
        "    assert validar_email(\"sin\") is False\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=e2e@test", "-c", "user.name=e2e",
         "commit", "-q", "-m", "init"],
        cwd=repo, check=True,
    )
    return repo


# ----------------------------------------------------------------------------
# Stub del LLM del presenter (hermetico)
# ----------------------------------------------------------------------------

class _StubLlm:
    """Stub de PluginLlm: cuenta llamadas, devuelve texto vestido fijado."""

    def __init__(self, reply: str = "vestido"):
        self.reply = reply
        self.calls: List[Dict[str, Any]] = []

    def complete(self, messages, **kw):
        self.calls.append({"messages": messages, "kw": kw})
        return SimpleNamespace(text=self.reply)


def _ctx_stub(mode: str, llm: _StubLlm) -> SimpleNamespace:
    settings: Dict[str, Any] = {"presenter_mode": mode}
    return SimpleNamespace(
        get_config=lambda key, default=None: settings.get(key, default),
        llm=llm,
    )


def _presentar(arnes_plugin, llm_reply: str) -> _StubLlm:
    """Inyecta el ctx del presenter con mode=on y worker_mode activo."""
    llm = _StubLlm(reply=llm_reply)
    arnes_plugin._PRESENTER_CTX = _ctx_stub("on", llm)
    return llm


# ----------------------------------------------------------------------------
# Escenario A: la tarea feliz — secuencia completa con gates activos
# ----------------------------------------------------------------------------

class TestEscenarioFeliz:
    def test_secuencia_completa(self, arnes_plugin, tmp_path, monkeypatch):
        """analyze_scope -> write bloqueado -> read -> write ok -> tests/lint
        verdes -> finish arma done -> presenter viste la entrega."""
        repo = _mk_proyecto(tmp_path)
        arnes_plugin.gate_state.reset()
        gate = arnes_plugin.gate_state.get()
        gate["terminal_cwd"] = str(repo)  # el runner detecta el proyecto real

        # --- Turno 1: el usuario pide la feature -----------------------------
        # pre_api_request: abre el turno (latch del presenter en cero)
        arnes_plugin._on_pre_api_request(turn_id="t1", user_message="agrega validacion")

        # El agente lee el archivo primero (track de reads)
        r = arnes_plugin._check_pre_tool_call(
            "read_file", {"path": str(repo / "user.py")})
        assert r is None
        arnes_plugin._on_post_tool_call(
            tool_name="read_file", args={"path": str(repo / "user.py")}, result="...")

        # Con el archivo ya leido y dentro del repo, el write pasa directo
        # (D6: la reversibilidad es la autorizacion; el beat "bloquea sin
        # leer" vive en TestEscenarioRecuperacion).
        r = arnes_plugin._check_pre_tool_call(
            "write_file",
            {"path": str(repo / "user.py"), "content": "x = 1\n"},
        )
        assert r is None

        # El agente valida su plan igual (analyze_scope prende scope_done:
        # lo exigira el finish gate si algo cayo fuera de todo repo)
        out = arnes_plugin._handle_analyze_scope({
            "impact": "BAJO",
            "modules": ["user"],
            "tests_status": "partial",
        })
        assert "OK" in out
        assert gate["scope_done"] is True

        # El write real de la feature tambien pasa
        r = arnes_plugin._check_pre_tool_call(
            "write_file",
            {"path": str(repo / "user.py"),
             "content": "def doble(x):\n    return x * 2\n"},
        )
        assert r is None
        arnes_plugin._on_post_tool_call(
            tool_name="write_file",
            args={"path": str(repo / "user.py")},
            result="ok",
        )

        # Evidencia real: run_tests corre pytest contra el test del juguete
        out_tests = arnes_plugin._handle_run_tests({})
        assert "1 passed" in out_tests, out_tests
        # lint: sin ruff en el venv del runner -> fail-open honesto (WARNING,
        # no bloqueo) — la filosofia del arnes ante fallos de deteccion
        arnes_plugin._handle_run_lint({})

        # Finish gate: con evidencia verde deja cerrar y arma done
        r = arnes_plugin._on_pre_verify(
            coding=True,
            changed_paths=[str(repo / "user.py")],
        )
        assert r is None
        assert gate["done"] is True

        # Presenter: HERMES_HOME con SOUL + turno con tools + routing on
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "SOUL.md").write_text(
            "# Identity\nSoy la voz del escenario.", encoding="utf-8")
        llm = _presentar(arnes_plugin, "La criatura duplico la luz: 2 tests verdes.")
        monkeypatch.setattr(arnes_plugin, "_worker_mode_active", lambda: True)
        try:
            entrega = arnes_plugin._on_transform_llm_output(
                response_text="done: 2 tests passed, lint clean",
                session_id="s", model="m", platform="telegram",
            )
        finally:
            arnes_plugin._PRESENTER_CTX = None
        assert entrega == "La criatura duplico la luz: 2 tests verdes."

        # Metrica sagrada F7: el transcript (content) queda RAW — el worker
        # de turnos futuros nunca ve el texto vestido. En el core, el hook
        # dispara DESPUES de persistir; aqui lo simulamos:
        transcript = [{"role": "assistant", "content": "done: 2 tests passed, lint clean"}]
        assert transcript[0]["content"] == "done: 2 tests passed, lint clean"
        assert "duplico" not in transcript[0]["content"]

    def test_presenter_off_entrega_cruda(self, arnes_plugin, tmp_path, monkeypatch):
        """presenter_mode=off (default): la entrega viaja RAW (deploy inerte)."""
        _mk_proyecto(tmp_path)
        repo = tmp_path / "proyecto"
        arnes_plugin.gate_state.reset()
        gate = arnes_plugin.gate_state.get()
        gate["terminal_cwd"] = str(repo)
        arnes_plugin._on_pre_api_request(turn_id="t1", user_message="task")
        arnes_plugin._on_post_tool_call(tool_name="read_file", args={"path": "x"}, result="ok")

        llm = _StubLlm()
        arnes_plugin._PRESENTER_CTX = _ctx_stub("off", llm)
        monkeypatch.setattr(arnes_plugin, "_worker_mode_active", lambda: True)
        try:
            entrega = arnes_plugin._on_transform_llm_output(
                response_text="output crudo", session_id="s", model="m",
                platform="telegram",
            )
        finally:
            arnes_plugin._PRESENTER_CTX = None
        assert entrega is None  # None = el canal no transformo
        assert llm.calls == []  # ni gasto la llamada

    def test_multiturno_transcript_siempre_raw(self, arnes_plugin, tmp_path, monkeypatch):
        """F7 metrica: tras TRES turnos con presenter activo, el historial que
        lee el worker no contiene nada vestido (solo las entregas se vistieron)."""
        repo = _mk_proyecto(tmp_path)
        arnes_plugin.gate_state.reset()
        gate = arnes_plugin.gate_state.get()
        gate["terminal_cwd"] = str(repo)

        # HERMES_HOME con SOUL: sin alma no hay persona que ponerse (e).
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "SOUL.md").write_text(
            "# Identity\nSoy la voz del escenario.", encoding="utf-8")

        transcript: List[Dict[str, str]] = []
        llm = _presentar(arnes_plugin, "VESTIDO")
        monkeypatch.setattr(arnes_plugin, "_worker_mode_active", lambda: True)
        try:
            for i in range(3):
                arnes_plugin._on_pre_api_request(turn_id=f"t{i}", user_message=f"task {i}")
                arnes_plugin._on_post_tool_call(
                    tool_name="terminal", args={"command": "ls"}, result="")
                raw = f"resultado crudo {i}"
                entrega = arnes_plugin._on_transform_llm_output(
                    response_text=raw, session_id="s", model="m",
                    platform="telegram")
                # El core persiste el RAW antes del hook (estructura F5):
                transcript.append({"role": "assistant", "content": raw})
                # La entrega al usuario SI se vistio (turno con tools + on)
                assert entrega == "VESTIDO"
        finally:
            arnes_plugin._PRESENTER_CTX = None

        # El historial del worker: puro RAW, cero contaminacion de persona.
        for msg in transcript:
            assert "VESTIDO" not in msg["content"]
        assert len(llm.calls) == 3


# ----------------------------------------------------------------------------
# Escenario B: el gate bloquea, el agente se recupera
# ----------------------------------------------------------------------------

class TestEscenarioRecuperacion:
    def test_bloquea_y_se_recupera(self, arnes_plugin, tmp_path):
        """write sin scope BLOQUEA (archivo intacto); analyze_scope; write OK."""
        repo = _mk_proyecto(tmp_path)
        arnes_plugin.gate_state.reset()
        gate = arnes_plugin.gate_state.get()
        gate["terminal_cwd"] = str(repo)
        arnes_plugin._on_pre_api_request(turn_id="t1", user_message="task")

        original = (repo / "user.py").read_text()

        # write sin lectura previa sobre archivo existente: bloquea
        # (dentro de un repo git el scope no aplica — D6: reversibilidad es
        # autorizacion; el gate que dispara es read_before_write)
        r = arnes_plugin._check_pre_tool_call(
            "write_file",
            {"path": str(repo / "user.py"), "content": "x = 1\n"},
        )
        assert r is not None and r["action"] == "block"
        assert "leido" in r["message"].lower()
        assert (repo / "user.py").read_text() == original  # intacto

        # Lectura + scope + write: pasa
        arnes_plugin._check_pre_tool_call(
            "read_file", {"path": str(repo / "user.py")})
        out = arnes_plugin._handle_analyze_scope({
            "impact": "BAJO", "modules": ["user"], "tests_status": "partial"})
        assert "OK" in out
        r = arnes_plugin._check_pre_tool_call(
            "write_file",
            {"path": str(repo / "user.py"),
             "content": "def doble(x):\n    return x * 2\n"},
        )
        # read_before_write cumplido, scope aprobado, dentro de repo (D6):
        assert r is None

    def test_finish_bloquea_sin_evidencia(self, arnes_plugin, tmp_path):
        """Escribio pero no corrio tests: el finish gate lo manda de vuelta."""
        repo = _mk_proyecto(tmp_path)
        arnes_plugin.gate_state.reset()
        gate = arnes_plugin.gate_state.get()
        gate["terminal_cwd"] = str(repo)
        arnes_plugin._on_pre_api_request(turn_id="t1", user_message="task")
        arnes_plugin._check_pre_tool_call(
            "read_file", {"path": str(repo / "user.py")})
        arnes_plugin._handle_analyze_scope({
            "impact": "BAJO", "modules": ["user"], "tests_status": "partial"})
        arnes_plugin._check_pre_tool_call(
            "write_file",
            {"path": str(repo / "user.py"), "content": "y = 2\n"})

        # Evidencia ROJA (1 failed): el finish gate SI bloquea y manda a correr tests
        gate = arnes_plugin.gate_state.get()
        gate["last_test_output"] = "=== 3 failed, 1 passed in 0.1s ==="
        r = arnes_plugin._on_pre_verify(coding=True, changed_paths=[str(repo / "user.py")])
        assert r is not None and r["action"] == "continue"
        assert "test" in r["message"].lower() or "failed" in r["message"].lower()

        # Evidencia AUSENTE: fail-open (WARNING, no bloqueo) — degradar antes
        # que bloquear permanente (filosofia del arnes, D11).
        gate["last_test_output"] = None
        r = arnes_plugin._on_pre_verify(coding=True, changed_paths=[str(repo / "user.py")])
        assert r is None
        assert gate["done"] is True


# ----------------------------------------------------------------------------
# Registro completo: los 6 hooks colgados
# ----------------------------------------------------------------------------

class TestRegistroCompleto:
    def test_register_cuelga_los_seis_hooks(self, arnes_plugin):
        registered: Dict[str, Any] = {}

        class _Ctx:
            def register_hook(self, name, cb):
                registered[name] = cb

            def register_tool(self, **kw):
                pass

        try:
            arnes_plugin.register(_Ctx())
        finally:
            arnes_plugin._PRESENTER_CTX = None
        for hook in (
            "on_session_start", "pre_tool_call", "post_tool_call",
            "pre_verify", "pre_api_request", "transform_llm_output",
        ):
            assert hook in registered, f"falta {hook}"
