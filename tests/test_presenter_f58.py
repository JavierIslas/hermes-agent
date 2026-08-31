"""F5.8: memoria del vestidor + registro del par raw/vestido.

Dos piezas (propuesta aprobada 2026-08-28, ver bóveda arnes-gates.md):

1. MEMORIA DEL VESTIDOR: el presenter es one-shot (PluginLlm stateless);
   sin memoria no tiene continuidad de voz ni ve las correcciones del
   usuario ("no me hables así" llega al worker, no al vestidor). El buffer
   es PROPIO y curado (última vestida + último mensaje del usuario), NO la
   conversación: el vestidor viste, no conversa.

2. REGISTRO DEL PAR: para el visor worker/presenter (dashboard plugin)
   hace falta el par (raw, dressed) persistido. El core NO lo persiste
   (persist-then-transform: el transcript queda crudo por diseño). El
   plugin lo registra: JSONL bounded en HERMES_HOME, append por turno.

Contratos testeados:
- pre_api_request guarda user_message en gate_state (turno nuevo, patron D7).
- _presenter_record_pair escribe el JSONL: raw+dressed o raw+razón fail-open.
- _build_dress_output_prompt inyecta el bloque de memoria ANTES del snapshot.
- El registro jamás rompe el turno (fail-open total, como todo el presenter).
- Bounded: el JSONL se capa a N entradas (rotación).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

# Fixture arnes_plugin: definida en tests/conftest.py.


class _StubLlm:
    """Stub de la fachada PluginLlm (mismo shape que test_presenter.py)."""

    def __init__(self, reply: str = "vestido", fail: bool = False):
        self.reply = reply
        self.fail = fail
        self.calls: List[Dict[str, Any]] = []

    def complete(self, messages, **kw):
        self.calls.append({"messages": messages, "kw": kw})
        if self.fail:
            raise RuntimeError("llm down")
        import types
        return types.SimpleNamespace(text=self.reply)


def _mk_soul(home: Path, worker_soul: str = "# Identity\nSoy la voz del escenario.") -> None:
    (home / "SOUL_WORKER.md").write_text(worker_soul, encoding="utf-8")


def _reset_gate(arnes_plugin):
    arnes_plugin.gate_state.reset()


# ============================================================================
# 1. user_message en gate_state (via pre_api_request, patron D7)
# ============================================================================

class TestUserMessageCapture:
    def test_turno_nuevo_guarda_user_message(self, arnes_plugin):
        """pre_api_request con turn_id nuevo: user_message queda en gate_state."""
        _reset_gate(arnes_plugin)
        arnes_plugin._on_pre_api_request(
            turn_id="t1", user_message="dale Maria usá develop, criatura")
        gate = arnes_plugin.gate_state.get()
        assert gate["presenter_user_message"] == \
            "dale Maria usá develop, criatura"

    def test_turno_mismo_no_pisa_user_message(self, arnes_plugin):
        """Dentro del mismo turn_id (retries/tool loops) el user_message no
        se re-guarda: el primero manda (es EL mensaje del usuario)."""
        _reset_gate(arnes_plugin)
        arnes_plugin._on_pre_api_request(turn_id="t1", user_message="msg original")
        # misma llamada de turno, user_message viene vacio en retries
        arnes_plugin._on_pre_api_request(turn_id="t1", user_message="")
        gate = arnes_plugin.gate_state.get()
        assert gate["presenter_user_message"] == "msg original"

    def test_turno_nuevo_pisa_el_anterior(self, arnes_plugin):
        """Turno nuevo = mensaje nuevo."""
        _reset_gate(arnes_plugin)
        arnes_plugin._on_pre_api_request(turn_id="t1", user_message="uno")
        arnes_plugin._on_pre_api_request(turn_id="t2", user_message="dos")
        gate = arnes_plugin.gate_state.get()
        assert gate["presenter_user_message"] == "dos"


# ============================================================================
# 2. Registro del par (JSONL bounded)
# ============================================================================

class TestPairRecord:
    def test_vestida_registra_par_completo(self, arnes_plugin, tmp_path, monkeypatch):
        """Turno vestido: JSONL con raw, dressed, user_message, ts, tools."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _mk_soul(tmp_path)
        _reset_gate(arnes_plugin)
        arnes_plugin._on_pre_api_request(
            turn_id="t1", user_message="arreglá el presenter")
        arnes_plugin.gate_state.get()["presenter_tool_calls"] = 3
        llm = _StubLlm(reply="El ritual quedó sellado en /tmp/x.py, criatura.")
        p = arnes_plugin.presenter.Presenter(llm)
        out = p.present("Cambié /tmp/x.py")

        arnes_plugin._presenter_record_pair(
            raw="Cambié /tmp/x.py", dressed=out, user_message="arreglá el presenter",
            tool_calls=3)
        f = tmp_path / "presenter_pairs.jsonl"
        assert f.exists()
        entry = json.loads(f.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert entry["raw"] == "Cambié /tmp/x.py"
        assert entry["dressed"] == out
        assert entry["user_message"] == "arreglá el presenter"
        assert entry["tool_calls"] == 3
        assert entry["outcome"] == "dressed"
        assert entry["ts"]

    def test_failopen_registra_la_razon(self, arnes_plugin, tmp_path, monkeypatch):
        """Fail-open: el par se registra igual con outcome=failopen:<razón> —
        el visor necesita mostrar POR QUÉ llegó crudo."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _mk_soul(tmp_path)
        _reset_gate(arnes_plugin)
        llm = _StubLlm(reply="respuesta sin el path")  # pierde /tmp/x.py
        p = arnes_plugin.presenter.Presenter(llm)
        raw = "Cambié /tmp/x.py"
        out = p.present(raw)  # fail-open: devuelve crudo

        arnes_plugin._presenter_record_pair(
            raw=raw, dressed=out, user_message="", tool_calls=1,
            failopen_reason="integrity")
        f = tmp_path / "presenter_pairs.jsonl"
        entry = json.loads(f.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert entry["outcome"] == "failopen:integrity"
        assert entry["dressed"] == raw  # crudo

    def test_registro_bounded_rota(self, arnes_plugin, tmp_path, monkeypatch):
        """El JSONL se capa a _PAIR_RECORD_MAX entradas: append + rotación."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _reset_gate(arnes_plugin)
        for i in range(arnes_plugin._PAIR_RECORD_MAX + 5):
            arnes_plugin._presenter_record_pair(
                raw=f"raw {i}", dressed=f"dressed {i}",
                user_message=f"msg {i}", tool_calls=1)
        f = tmp_path / "presenter_pairs.jsonl"
        lines = f.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == arnes_plugin._PAIR_RECORD_MAX
        # los más viejos se cayeron, el más nuevo quedó
        first = json.loads(lines[0])
        last = json.loads(lines[-1])
        assert first["raw"] != "raw 0"
        assert last["raw"] == f"raw {arnes_plugin._PAIR_RECORD_MAX + 4}"

    def test_registro_jamas_truena(self, arnes_plugin, tmp_path, monkeypatch):
        """Disco roto/invisible: el registro falla en silencio (fail-open
        total del presenter — el teatro jamás rompe un turno de ingeniería)."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "no" / "existe"))
        _reset_gate(arnes_plugin)
        # no truena:
        arnes_plugin._presenter_record_pair(
            raw="r", dressed="d", user_message="", tool_calls=0)


# ============================================================================
# 3. Memoria en el prompt del vestidor (via el hook, como en produccion)
# ============================================================================

def _hook_on(arnes_plugin, monkeypatch, tmp_path, reply="vestido ok",
             user_message=None, last_dressed=None, tool_calls=2):
    """Prende el circuito del presenter con memoria F5.8 sembrada."""
    from types import SimpleNamespace
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _mk_soul(tmp_path)
    gate = arnes_plugin.gate_state.get()
    gate["presenter_tool_calls"] = tool_calls
    if user_message is not None:
        gate["presenter_user_message"] = user_message
    if last_dressed is not None:
        gate["presenter_last_dressed"] = last_dressed
    settings = {"presenter_mode": "on"}
    arnes_plugin._PRESENTER_CTX = SimpleNamespace(
        get_config=lambda key, default=None: settings.get(key, default),
        llm=_StubLlm(reply=reply),
    )
    monkeypatch.setattr(arnes_plugin, "_worker_mode_active", lambda: True)


class TestDresserMemory:
    def test_bloque_memoria_antes_del_snapshot(self, arnes_plugin, tmp_path, monkeypatch):
        """El prompt de vestir lleva la última vestida + el mensaje del
        usuario desde entonces (via hook, como en producción)."""
        _reset_gate(arnes_plugin)
        _hook_on(arnes_plugin, monkeypatch, tmp_path, reply="ok con /tmp/a.py",
                 user_message="no me hables de rituales",
                 last_dressed="El ritual quedó sellado, criatura.")
        llm = arnes_plugin._PRESENTER_CTX.llm  # el stub que el hook usa
        arnes_plugin._on_transform_llm_output(
            response_text=" cambié /tmp/a.py ", session_id="s", platform="telegram")
        prompt_text = "".join(str(m) for m in llm.calls[0]["messages"])
        assert "# Memoria del vestidor" in prompt_text
        assert "El ritual quedó sellado, criatura." in prompt_text
        assert "no me hables de rituales" in prompt_text

    def test_sin_memoria_no_hay_bloque(self, arnes_plugin):
        """Primera vestida de la sesión: sin bloque de memoria (prompt
        idéntico al de F5.4 — retrocompatible con el stub echo del E2E)."""
        _reset_gate(arnes_plugin)
        prompt = arnes_plugin.presenter._build_dress_output_prompt("texto")
        assert "# Memoria del vestidor" not in prompt

    def test_vestida_actualiza_last_dressed(self, arnes_plugin, tmp_path, monkeypatch):
        """Tras vestir via el hook, la vestida queda como memoria y el
        user_message atendido se limpia."""
        _reset_gate(arnes_plugin)
        _hook_on(arnes_plugin, monkeypatch, tmp_path,
                 reply="Vestida del turno con /tmp/x.py, criatura.",
                 user_message="arreglá el presenter")
        out = arnes_plugin._on_transform_llm_output(
            response_text="cambié /tmp/x.py", session_id="s", platform="telegram")
        assert out == "Vestida del turno con /tmp/x.py, criatura."
        gate = arnes_plugin.gate_state.get()
        assert gate.get("presenter_last_dressed") == \
            "Vestida del turno con /tmp/x.py, criatura."
        assert gate.get("presenter_user_message") is None

    def test_failopen_no_contamina_memoria(self, arnes_plugin, tmp_path, monkeypatch):
        """Un fail-open NO deja el crudo como 'última vestida': la memoria
        es de VESTIDAS, no de entregas. Y registra el par con la razón."""
        import json as _json
        _reset_gate(arnes_plugin)
        _hook_on(arnes_plugin, monkeypatch, tmp_path,
                 reply="respuesta sin el path",  # pierde /tmp/x.py -> failopen
                 user_message="msg", last_dressed="vestida previa buena")
        out = arnes_plugin._on_transform_llm_output(
            response_text="cambié /tmp/x.py", session_id="s", platform="telegram")
        assert out is None  # crudo
        gate = arnes_plugin.gate_state.get()
        assert gate.get("presenter_last_dressed") == "vestida previa buena"
        # y el par quedó registrado con la razón del fail-open
        f = tmp_path / "presenter_pairs.jsonl"
        entry = _json.loads(f.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert entry["outcome"] == "failopen:integrity"
        assert entry["dressed"] == "cambié /tmp/x.py"

