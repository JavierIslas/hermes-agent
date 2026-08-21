"""Plugin arnes-gates: torre de verificacion estructural.

Registra hooks que interceptan tools mutativas (write_file, patch) y un hook
pre_verify que exige tests+lint+typecheck antes de cerrar una task de codigo.
La tool analyze_scope es el gate de plan: el modelo la llama antes de escribir
para validar su plan y desbloquear la escritura.

Filosofia (heredada de agent_juguete): la autoridad vive en las tools y el
estado del proceso, no en el texto. Los gates son codigo que el modelo no puede
tocar. El system prompt es advisory; los gates son duros.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import state as gate_state
from . import index_service
from . import embedding_service
from . import memory_scope
from .gates import analyze_scope
from .retrieval import find_references, search_semantic
from .reuse_gates import validar_reuse_en_diff
from .dup_gates import validar_duplicacion, validar_duplicacion_semantica
from .terminal_guard import detect_write_command, should_block, extract_write_targets, parse_cd, detect_git_commit
from .verification import run_tests, run_lint, run_typecheck
from . import presenter as presenter_mod

logger = logging.getLogger(__name__)

# PluginContext del registro (Fase 5): el presenter lo usa para ctx.llm y
# ctx.get_config. None hasta que register() corre (tests: se inyecta directo).
_PRESENTER_CTX: Any = None


# =============================================================================
# Schemas de tools (formato OpenAI function, como las espera Hermes).
# =============================================================================
ANALYZE_SCOPE_SCHEMA: dict[str, Any] = {
    "name": "analyze_scope",
    "description": (
        "Valida tu plan de cambio y desbloquea la escritura. Debes llamarlo "
        "antes de write_file o patch. Recibe campos estructurados (no prosa) "
        "y los valida: impact, modules, tests_status. Si todo calza, prende "
        "scope_done y ya podes escribir codigo."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "impact": {
                "type": "string",
                "enum": ["BAJO", "MEDIO", "ALTO"],
                "description": "Magnitud del impacto del cambio.",
            },
            "modules": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Modulos afectados (lista no vacia).",
            },
            "tests_status": {
                "type": "string",
                "enum": ["covered", "partial", "none"],
                "description": (
                    "Estado de la cobertura de tests. 'none' exige TDD."
                ),
            },
        },
        "required": ["impact", "modules", "tests_status"],
    },
}


FIND_REFERENCES_SCHEMA: dict[str, Any] = {
    "name": "find_references",
    "description": (
        "Encuentra dónde se define y dónde se usa un símbolo en el codebase. "
        "Consulta el índice estructural (AST): distingue el símbolo de texto "
        "igual en comentarios/strings. Es el complemento estructural de "
        "search_files."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Nombre del símbolo a buscar (case-sensitive).",
            },
        },
        "required": ["symbol"],
    },
}


SEARCH_SEMANTIC_SCHEMA: dict[str, Any] = {
    "name": "search_semantic",
    "description": (
        "Busca símbolos del codebase por similitud léxica (TF-IDF). Útil para "
        "localizar por concepto o identifier compartido cuando no sabés el "
        "nombre exacto. Rankea por score de similitud."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Concepto o texto a buscar.",
            },
            "top_k": {
                "type": "integer",
                "description": "Máximo número de resultados (default: 8).",
                "default": 8,
            },
        },
        "required": ["query"],
    },
}


RUN_TESTS_SCHEMA: dict[str, Any] = {
    "name": "run_tests",
    "description": (
        "Corre los tests del proyecto y prende el flag tests_green si pasan "
        "(rc=0). El finish gate exige tests_green antes de cerrar una task "
        "de coding. El runner se detecta del filesystem (pytest, npm, go, "
        "cargo, make)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Path opcional para limitar qué tests correr.",
            },
        },
        "required": [],
    },
}


RUN_LINT_SCHEMA: dict[str, Any] = {
    "name": "run_lint",
    "description": (
        "Corre el linter del proyecto y prende el flag lint_green si pasa "
        "(rc=0). El finish gate exige lint_green antes de cerrar. El linter "
        "se detecta del filesystem (ruff, flake8, eslint)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Path opcional a lintear (default: todo el proyecto).",
            },
        },
        "required": [],
    },
}


RUN_TYPECHECK_SCHEMA: dict[str, Any] = {
    "name": "run_typecheck",
    "description": (
        "Corre el type-checker del proyecto y prende typecheck_green si pasa "
        "(rc=0). El finish gate exige typecheck_green antes de cerrar. Si no "
        "hay type-checker declarado (mypy), el gate lo saltea (opt-in)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Path opcional a typechequear.",
            },
        },
        "required": [],
    },
}


# =============================================================================
# Handlers de tools.
# =============================================================================
def _handle_analyze_scope(args: Dict[str, Any], **_kw: Any) -> str:
    """Handler de la tool analyze_scope: delega al gate real."""
    return analyze_scope(
        impact=args.get("impact", ""),
        modules=args.get("modules") or [],
        tests_status=args.get("tests_status", ""),
    )


def _handle_find_references(args: Dict[str, Any], **_kw: Any) -> str:
    """Handler de la tool find_references: delega al retrieval."""
    return find_references(symbol=args.get("symbol", ""))


def _handle_search_semantic(args: Dict[str, Any], **_kw: Any) -> str:
    """Handler de la tool search_semantic: delega al retrieval."""
    return search_semantic(
        query=args.get("query", ""),
        top_k=args.get("top_k", 8),
    )


def _handle_run_tests(args: Dict[str, Any], **_kw: Any) -> str:
    """Handler de la tool run_tests."""
    return run_tests(target=args.get("target"))


def _handle_run_lint(args: Dict[str, Any], **_kw: Any) -> str:
    """Handler de la tool run_lint."""
    return run_lint(target=args.get("target"))


def _handle_run_typecheck(args: Dict[str, Any], **_kw: Any) -> str:
    """Handler de la tool run_typecheck."""
    return run_typecheck(target=args.get("target"))


# =============================================================================
# Hooks (stubs — se implementan en Tasks 1.3 y 1.4).
# =============================================================================

# =============================================================================
# Fase 5: presenter — vestir la entrega sin contaminar al worker
# =============================================================================
# Routing (las cinco condiciones, diseno 2026-08-18):
#   (a) presenter_mode != "off" — default OFF: deploy inerte, se activa con
#       plugins.entries.arnes-gates.settings.presenter_mode: on
#   (b) worker_mode activo (config del core: agent.worker_mode) — si el modelo
#       YA habla con la persona en su system prompt, vestir seria doble
#   (c) platform != "subagent" — los hijos de delegate_task corren con
#       platform="subagent": la persona no contamina sus resumenes
#   (d) el turno uso tools — charla pura no se viste
#   (e) SOUL.md existe y el output no esta vacio (verificado dentro de
#       Presenter.present, fail-open)


def _presenter_mode() -> str:
    """Lee presenter_mode de la config del plugin, normalizado a
    on | on_finish | off.

    Acepta las formas naturales de YAML (dogfood 2026-08-18): bool
    ``true``/``false`` (true = on: vestir cada turno con tools), y strings
    ``on``/``on_finish``/``off``/``true``/``false`` (case-insensitive).
    F5.3 (2026-08-21): ``on_finish`` viste SOLO el turno que cierra la tarea
    (Rift 5): la latencia de vestir se paga una vez por tarea, no por turno.
    Cualquier otro valor o ausencia = off (inerte). Nunca trona: config
    ilegible → off.
    """
    if _PRESENTER_CTX is None:
        return "off"
    try:
        mode = _PRESENTER_CTX.get_config("presenter_mode", "off")
    except Exception:
        return "off"
    if isinstance(mode, bool):
        return "on" if mode else "off"
    if isinstance(mode, str):
        m = mode.strip().lower()
        if m == "on_finish":
            return "on_finish"
        return "on" if m in ("on", "true", "1") else "off"
    return "off"


def _worker_mode_active() -> bool:
    """True si agent.worker_mode esta prendido en la config del core.

    Lectura directa de config (load_config_readonly), sin referencia al
    agente vivo: el hook no recibe el agente, y la config no cambia
    mid-session. Default False (presenter inerte sin worker_mode).
    """
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        agent_section = cfg.get("agent") if isinstance(cfg, dict) else None
        return bool(agent_section.get("worker_mode", False)) if isinstance(agent_section, dict) else False
    except Exception:
        return False


def _presenter_enabled_for(platform: str) -> bool:
    """Condiciones (a)+(b)+(c): config + anti double-dressing + no subagent."""
    if _presenter_mode() == "off":
        return False
    if not _worker_mode_active():
        return False
    if platform and platform == "subagent":
        return False
    return True


# Mapeo evento → contador de fail-open (F5.3: telemetría del vestidor).
_PRESENTER_FAILOPEN_COUNTERS = {
    "failopen:integrity": "presenter_failopen_integrity",
    "failopen:llm": "presenter_failopen_llm",
    "failopen:timeout": "presenter_failopen_timeout",
    "failopen:empty": "presenter_failopen_empty",
}


def _presenter_on_event(event: str) -> None:
    """Telemetría del vestidor: cuenta dressed y fail-opens en gate_state.

    Dogfood D13: 2/3 vestidos morían en timeout y solo quedaba un WARNING de
    log invisible. Con contadores, on_session_end puede reportar la salud
    real del presenter (un presenter que siempre fail-openea es teatro
    muerto que solo agrega latencia — hay que poder verlo).
    """
    try:
        gate = gate_state.get()
        if event == "dressed":
            gate["presenter_dressed_count"] = gate.get("presenter_dressed_count", 0) + 1
            return
        key = _PRESENTER_FAILOPEN_COUNTERS.get(event)
        if key:
            gate[key] = gate.get(key, 0) + 1
    except Exception:  # pragma: no cover - telemetría jamás rompe
        logger.debug("presenter: on_event falló (ignorado)", exc_info=True)


# =============================================================================
# F5.4: snapshot del turno — hechos estructurados para el vestidor.
# =============================================================================

def _presenter_tests_summary(output: Optional[str]) -> Optional[str]:
    """Resumen corto del output de tests: '12 passed' | '3 failed, 10 passed'."""
    if not output:
        return None
    m_pass = re.search(r"(\d+)\s+passed", output)
    m_fail = re.search(r"(\d+)\s+failed", output)
    if m_fail and int(m_fail.group(1)) > 0:
        base = f"{m_fail.group(1)} failed"
        if m_pass:
            base += f", {m_pass.group(1)} passed"
        return base
    if m_pass and int(m_pass.group(1)) > 0:
        return f"{m_pass.group(1)} passed"
    return None


def _presenter_record_verdict(tool_name: str) -> None:
    """F5.4: registra el veredicto de la tool de verificación ejecutada.

    Re-evalúa la evidencia cruda que el handler ya guardó en
    last_*_output — el mismo código que el finish gate usa, cero parsing
    nuevo. Solo herramientas arnes-gates (run_tests/run_lint/run_typecheck):
    pytest via terminal crudo no produce evidencia estructurada de todos
    modos (el finish gate tampoco la acepta).
    """
    try:
        gate = gate_state.get()
        if tool_name == "run_tests":
            summary = _presenter_tests_summary(gate.get("last_test_output"))
            gate["presenter_turn_tests"] = summary
        elif tool_name == "run_lint":
            output = gate.get("last_lint_output") or ""
            from .evidence import evaluate_lint_evidence
            ev = evaluate_lint_evidence(output)
            gate["presenter_turn_lint"] = "limpio" if ev.verdict == "pass" else (
                "con errores" if ev.verdict == "fail" else None
            )
        elif tool_name == "run_typecheck":
            output = gate.get("last_typecheck_output") or ""
            from .evidence import evaluate_typecheck_evidence
            ev = evaluate_typecheck_evidence(output)
            gate["presenter_turn_typecheck"] = "limpio" if ev.verdict == "pass" else (
                "con errores" if ev.verdict == "fail" else None
            )
    except Exception:  # pragma: no cover - snapshot jamás rompe el turno
        logger.debug("presenter: record_verdict falló (ignorado)", exc_info=True)


def _presenter_turn_snapshot() -> Dict[str, Any]:
    """Hechos estructurados del turno para el vestidor (F5.4).

    Regla de oro: SOLO datos del gate_state (escritos por hooks reales),
    nunca texto del modelo. Vacío si el turno no tiene nada digno de
    contexto — un dict vacío apaga el bloque en el prompt.
    """
    try:
        gate = gate_state.get()
        snap: Dict[str, Any] = {}
        files = sorted(gate.get("presenter_turn_files") or [])
        if files:
            snap["files"] = files
        for key, state_key in (
            ("tests", "presenter_turn_tests"),
            ("lint", "presenter_turn_lint"),
            ("typecheck", "presenter_turn_typecheck"),
        ):
            val = gate.get(state_key)
            if val:
                snap[key] = val
        if gate.get("presenter_finish_clean"):
            snap["finish_clean"] = True
        return snap
    except Exception:  # pragma: no cover - snapshot jamás rompe el turno
        logger.debug("presenter: snapshot falló (ignorado)", exc_info=True)
        return {}


def _on_transform_llm_output(
    response_text: str = "",
    session_id: str = "",
    model: str = "",
    platform: str = "",
    **_kw: Any,
) -> Optional[str]:
    """Hook transform_llm_output: viste SOLO la entrega al usuario.

    El core dispara esto una vez por turno, DESPUES de persistir el mensaje
    assistant (turn_finalizer.py): el transcript queda RAW — el worker de
    turnos futuros nunca ve el texto vestido. Contrato del canal: primer
    string no-vacio gana; None = no transformar.
    """
    gate = gate_state.get()
    if not _presenter_enabled_for(platform):
        return None
    if gate.get("presenter_tool_calls", 0) <= 0:
        return None  # (d) charla pura: no se viste
    if _presenter_mode() == "on_finish" and not gate.get("presenter_finish_clean"):
        return None  # Rift 5: turno intermedio — solo se viste el cierre
    if _PRESENTER_CTX is None:
        return None
    try:
        p = presenter_mod.Presenter(
            _PRESENTER_CTX.llm,
            get_config=_PRESENTER_CTX.get_config,
            on_event=_presenter_on_event,
        )
    except Exception as exc:
        logger.debug("presenter: ctx.llm no disponible, sin vestir (%s)", exc)
        return None
    dressed = p.present(response_text, ctx_snapshot=_presenter_turn_snapshot())
    # present() es fail-open (devuelve el original ante cualquier fallo):
    # solo reportamos transformación si realmente cambió algo. El contador
    # dressed/fail-open lo lleva on_event (telemetría F5.3).
    if dressed and dressed.strip() and dressed.strip() != response_text.strip():
        logger.info(
            "arnes-gates: presenter vistio la entrega (turno con %d tools)",
            gate.get("presenter_tool_calls", 0),
        )
        return dressed
    return None


def _on_session_start(**_kw: Any) -> None:
    """Reset del estado de gates al iniciar sesion.

    NO construye el indice/embeddings al arrancar. El cwd del proceso de
    Hermes (/opt/hermes) casi nunca es el proyecto sobre el que el agente
    va a trabajar. Construir el indice de /opt/hermes (1107 archivos) toma
    42s la primera vez y es inútil si el agente trabaja en /workspace/...

    El indice se construye de forma LAZY: la primera vez que find_references
    o search_semantic se llama, o cuando el agente hace cd a un proyecto
    (detectado via parse_cd en post_tool_call).

    El cache en disco (load_or_build) hace que las construcciones subsecuentes
    del mismo proyecto tomen <1s (solo reconcile de archivos cambiados).
    """
    gate_state.reset()
    index_service.reset()
    embedding_service.reset()
    memory_scope.reset()
    logger.debug("arnes-gates: sesion iniciada — indice se construira bajo demanda")


def _on_session_end(**_kw: Any) -> None:
    """Resumen de salud del presenter al cerrar la sesión (F5.3).

    Telemetría de fail-open: dogfood D13 mostró un presenter que fallaba en
    2/3 de los vestidos (timeout) sin señal observable. Si los fail-opens
    dominan, el vestidor es teatro muerto que solo agrega latencia: bajar el
    output por sesión da el dato para decidir (presenter_model barato, más
    timeout, o apagar).
    """
    try:
        gate = gate_state.get()
        dressed = gate.get("presenter_dressed_count", 0)
        total_fail = sum(
            gate.get(k, 0)
            for k in _PRESENTER_FAILOPEN_COUNTERS.values()
        )
        if dressed or total_fail:
            detail = ", ".join(
                f"{k.split('_', 2)[2]}={gate.get(k, 0)}"
                for k in _PRESENTER_FAILOPEN_COUNTERS.values()
                if gate.get(k, 0)
            )
            logger.info(
                "arnes-gates: presenter sesion cerrada — vestidas=%d fail-opens=%d%s",
                dressed, total_fail, f" ({detail})" if detail else "",
            )
    except Exception:  # pragma: no cover - telemetría jamás rompe
        logger.debug("arnes-gates: resumen de presenter falló", exc_info=True)


def _on_pre_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    **_kw: Any,
) -> Optional[Dict[str, Any]]:
    """Hook pre_tool_call: intercepta tools mutativas (write gate) + circuit breaker.

    Devuelve {"action": "block", "message": "..."} para bloquear (Hermes lo
    convierte en el tool_result que ve el modelo). Devuelve None para permitir.

    Wrappea _check_pre_tool_call (la lógica de gates) para contar bloqueos
    consecutivos y activar el circuit breaker.
    """
    gate = gate_state.get()

    # Circuit breaker: si el arnés bloqueó N veces seguidas sin progreso,
    # bloquear TODAS las tools hasta que el usuario intervenga.
    # Fix D7 (dogfooding 2026-08-15): exentar analyze_scope y lectura — la
    # salida prescripta del propio breaker ("llama analyze_scope") debe ser
    # alcanzable desde dentro del turno, o el agente queda en deadlock.
    BLOCK_THRESHOLD = 5
    _EXEMPT_WHEN_TRIPPED = ("analyze_scope", "read_file", "search_files", "find_references")
    if gate.get("circuit_tripped") and tool_name not in _EXEMPT_WHEN_TRIPPED:
        return {
            "action": "block",
            "halt_loop": True,
            "message": (
                "CIRCUIT BREAKER: el arnés detectó que estás stuck después de "
                f"{BLOCK_THRESHOLD} bloqueos consecutivos sin progreso. "
                "Explicale al usuario qué te bloquea y esperá su respuesta "
                "(tu próximo mensaje rearma el breaker). "
                "Podés seguir leyendo (read_file/search_files/find_references) "
                "y pidiendo scope (analyze_scope) mientras esperás. "
                "No sigas intentando tools mutativas."
            ),
        }

    # Ejecutar la lógica de gates.
    result = _check_pre_tool_call(tool_name, args or {}, **_kw)

    # Actualizar block_count.
    if result is not None and result.get("action") == "block":
        gate["block_count"] += 1
        if gate["block_count"] >= BLOCK_THRESHOLD:
            gate["circuit_tripped"] = True
    else:
        # La tool pasó (no se bloqueó): resetear el contador.
        gate["block_count"] = 0

    return result




def _platform_kw(_kw: Dict[str, Any]) -> str:
    """Platform del pre_tool_call si viene en kwargs; "" si no.

    El dispatch de pre_tool_call no incluye platform (solo transform_llm_output
    lo recibe). Los subagentes corren sin clarify_callback (delegate_tool pasa
    callback=None), asi que clarify nunca ejecuta en platform=subagent: la
    condicion (c) queda cubierta estructuralmente para este branch.
    """
    val = _kw.get("platform", "")
    return val if isinstance(val, str) else ""


def _check_pre_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    **_kw: Any,
) -> Optional[Dict[str, Any]]:
    """Lógica de gates (write gate, read_before_write, terminal gate).

    Gates que aplica:
      1. scope_done: write_file/patch requieren analyze_scope previo.
      2. read_before_write: no pisar archivo existente sin haberlo leido.
      3. track reads: registrar paths leidos (read_file/search_files) para
         que el gate 2 funcione.
      4. track writes: registrar paths escritos (para el finish gate).
    """
    args = args or {}
    gate = gate_state.get()
    if tool_name in ("read_file", "search_files"):
        path = args.get("path", "")
        if path:
            gate["read_paths"].add(path)
            gate["ever_read"].add(path)
        return None

    # Fase 5 (Task 5.2): clarify vestido via modify directive.
    # El seam triada de D5 aplica modified_args ANTES del dispatch: la
    # plataforma renderiza la pregunta vestida y clarify_tool devuelve la
    # eleccion canonica (strip_recommended). Choices intactas: son canonicas.
    # Sin routing (off / sin worker_mode / sin tools en el turno): None.
    if tool_name == "clarify":
        question = args.get("question", "")
        if (
            question
            and _presenter_enabled_for(platform=_platform_kw(_kw))
            and gate.get("presenter_tool_calls", 0) > 0
            and _PRESENTER_CTX is not None
        ):
            try:
                pres = presenter_mod.Presenter(
                    _PRESENTER_CTX.llm,
                    get_config=_PRESENTER_CTX.get_config,
                )
                dressed = pres.dress_question(question)
            except Exception as exc:
                logger.debug("presenter: fail-open en clarify (%s)", exc)
                dressed = None
            if dressed and dressed.strip() and dressed.strip() != question.strip():
                return {"action": "modify", "args": {"question": dressed}}
        return None

    # Write gate: write_file y patch requieren scope + read_before_write.
    if tool_name in ("write_file", "patch"):
        path = args.get("path", "")

        # Gate 1: scope_done. Fix D6 (dogfooding 2026-08-15): el ritual
        # aplica solo a escrituras FUERA de todo repo git — dentro de un
        # repo la reversibilidad (git restore) ES la autorizacion. Los
        # demas gates (read_before_write, Q2-Q4) siguen aplicando igual.
        resolved = _resolve_write_path(gate, path) if path else None
        if not gate["scope_done"] and not (
            resolved is not None and _is_in_git_repo(resolved)
        ):
            return {
                "action": "block",
                "message": (
                    "BLOQUEADO: no hay scope aprobado. Llama analyze_scope "
                    "con un plan valido (impact, modules, tests_status) antes "
                    "de escribir codigo. (Si el destino vive dentro de un "
                    "repo git el ritual no aplica: la reversibilidad es la "
                    "autorizacion.)"
                ),
            }

        if path:
            # Gate 2: read_before_write (solo si el archivo existe — crear
            # uno nuevo esta permitido).
            if _exists_unread(gate, path):
                return {
                    "action": "block",
                    "message": (
                        f"BLOQUEADO: vas a MODIFICAR '{path}' sin haberlo "
                        f"leido. Leelo con read_file antes de pisarlo "
                        f"(para no perder lo que ya tiene)."
                    ),
                }
            # Track del write (para el finish gate: written_paths no vacio).
            gate["written_paths"].add(path)

            # Gates Q2-Q4: solo para archivos con adapter estructural.
            from .lang_adapter import get_adapter_for_path as _get_adapter
            if _get_adapter(path) is not None:
                content = args.get("content", "")
                # patch no tiene "content" pero si "new_string".
                if not content and tool_name == "patch":
                    content = args.get("new_string", "")
                error_reuse = validar_reuse_en_diff(content, gate["written_paths"])
                if error_reuse is not None:
                    return {"action": "block", "message": error_reuse}

                # Gate Q3: duplicación estructural (requiere indice).
                error_dup = validar_duplicacion(content, path)
                if error_dup is not None:
                    return {"action": "block", "message": error_dup}

                # Gate Q4: duplicación semántica (fail-open sin store).
                error_sem = validar_duplicacion_semantica(content, path)
                if error_sem is not None:
                    return {"action": "block", "message": error_sem}

    # Terminal gate: detectar writes via terminal (heredoc, redirect, cp, sed -i).
    # Cierra el escape hatch: si el modelo escribe via terminal en vez de
    # write_file/patch, exige el mismo scope_done.
    if tool_name == "terminal":
        cmd = args.get("command", "")

        # Gate Q5: commit gate — git commit requiere finish (done=True).
        if detect_git_commit(cmd):
            if not gate.get("done"):
                # Fix D11 (2026-08-18): evidencia fresca como alternativa a
                # done. El finish gate solo corre al CIERRE del turno, pero el
                # commit suele intentarce en el turno SIGUIENTE (el gate
                # bloqueó, el turno cerró sin prender done, el nuevo turno ya
                # no tiene edits → el finish gate ni se dispara). Sin esta
                # puerta, sesiones no-coding quedaban con bloqueo PERMANENTE.
                # La evidencia es la MISMA que el finish gate evalúa: scope
                # aprobado + tests pass + lint pass en el state del turno.
                from .evidence import evaluate_test_evidence, evaluate_lint_evidence

                _test_ev = evaluate_test_evidence(gate.get("last_test_output"))
                _lint_ev = evaluate_lint_evidence(gate.get("last_lint_output"))
                _fresh = (
                    gate.get("scope_done")
                    and _test_ev.verdict == "pass"
                    and _lint_ev.verdict == "pass"
                )
                if not _fresh:
                    return {
                        "action": "block",
                        "message": (
                            "BLOQUEADO: no podés commitear sin cerrar con finish "
                            "primero (falta scope aprobado o verificación de tests). "
                            "Asegurate de que el finish gate haya pasado antes de "
                            "materializar el cambio en git."
                        ),
                    }
                # Evidencia fresca: tratar como done — sigue el chequeo de
                # dangling imports abajo.
            # done=True (o evidencia fresca): verificar dangling imports si hay indice.
            from . import index_service
            index = index_service.get_index()
            root = index_service.get_root()
            if index is not None and root is not None:
                from .index import find_dangling
                dangling = find_dangling(index, root)
                if dangling:
                    detalle = "; ".join(
                        f"{d['importer']}:{d['lineno']} importa "
                        f"'{d.get('modulo', '?')}'"
                        f"{' (' + ', '.join(d['faltantes']) + ')' if d.get('faltantes') else ''}"
                        for d in dangling
                    )
                    return {
                        "action": "block",
                        "message": (
                            f"BLOQUEADO: {len(dangling)} import(s) colgante(s) "
                            f"— referencias a módulos o símbolos que ya no existen: "
                            f"{detalle}. Corregí los importadores antes de commitear."
                        ),
                    }

        detection = detect_write_command(cmd)
        if should_block(detection):
            # Fix D6: el ritual de scope aplica solo si ALGUNO de los
            # destinos vive fuera de todo repo git. Dentro de un repo la
            # reversibilidad es la autorizacion.
            targets = extract_write_targets(cmd)
            outside = [
                t for t in targets
                if not _resolve_write_path(gate, t)
                or not _is_in_git_repo(_resolve_write_path(gate, t))
            ]
            if not gate["scope_done"] and outside:
                return {
                    "action": "block",
                    "message": (
                        f"BLOQUEADO: el comando de terminal escribe archivos "
                        f"({detection.reason}) pero no hay scope aprobado. "
                        f"Llama analyze_scope con un plan valido antes de "
                        f"escribir codigo via terminal."
                    ),
                }

    return None


def _resolve_write_path(gate: dict, path: str):
    """Resuelve un path de escritura (relativo → terminal_cwd o cwd).

    Devuelve None si el path no se puede resolver: en ese caso D6 NO
    exime (fail-closed: el scope sigue aplicando).
    """
    from pathlib import Path

    try:
        p = Path(path).expanduser()
        if not p.is_absolute():
            base = gate.get("terminal_cwd") or str(Path.cwd())
            p = Path(base) / p
        return p
    except (OSError, ValueError):
        return None


def _is_in_git_repo(path) -> bool:
    """True si el path vive dentro de un repo git (algún ancestro tiene .git).

    Detección de FS (walk-up), sin subprocess: barata y hermética. Acepta
    .git como dir (repo normal) o file (worktree/submodule).
    """
    try:
        current = path.parent if path.is_file() or not path.exists() else path
        # Si no existe aún (archivo nuevo), partir del directorio contenedor.
        if current == path:
            current = path.parent
        for ancestor in (current, *current.parents):
            if (ancestor / ".git").exists():
                return True
        return False
    except (OSError, ValueError):
        return False


def _exists_unread(gate: dict, path: str) -> bool:
    """True si el archivo existe en FS y no fue leido en esta sesion."""
    from pathlib import Path

    try:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return False  # archivo nuevo: se permite crear
        # Existe: verificar si fue leido.
        return (
            path not in gate["read_paths"]
            and path not in gate["ever_read"]
        )
    except (OSError, ValueError):
        return False


def _on_pre_verify(
    coding: bool = False,
    changed_paths: Optional[list] = None,
    **_kw: Any,
) -> Optional[Dict[str, Any]]:
    """Hook pre_verify: finish gate.

    Se dispara cuando el agente edito codigo y esta por cerrar el turno.
    Devuelve {"action": "continue", "message": "..."} para obligar al agente
    a seguir (correr tests, etc.). Devuelve None para dejar cerrar.

    Gates que exige (si coding=True y changed_paths no vacio):
      1. scope_done: hubo plan aprobado.
      2. tests_green: los tests pasaron.
      3. lint_green: el linter paso.
      4. typecheck_green: el type-checker paso.

    El estado tests_green/lint_green/typecheck_green lo setea el modelo cuando
    corre tests/lint/typecheck via terminal. Como en Fase 1 no interceptamos
    terminal para detectar automaticamente el resultado, el gate confia en que
    el modelo reporte via la tool run_tests (TODO Fase 2) o lo setee manualmente.
    Por ahora, si no hay evidencia, bloquea guiando al modelo a correrlos.

    Fix D11 (2026-08-18): el early-return `if not coding` dejaba `done` muerto
    en sesiones no-coding (cwd fuera de todo workspace de código — p.ej. una
    sesión del agente que trabaja SOBRE el repo desde /opt/hermes), y el
    commit gate exigía done=True → bloqueo PERMANENTE. Ahora: sin
    changed_paths se deja cerrar como siempre; con changed_paths el ritual
    aplica IGUAL (scope para lo fuera-de-repo + evidencia), coding o no. La
    única diferencia es que coding=False no es excusa para saltarse el
    ritual: es excusa para nada, porque el ritual no depende del contexto.
    """
    if not changed_paths:
        return None  # nada cambiado: deja cerrar

    gate = gate_state.get()

    # Fix D6: el finish gate solo exige scope si alguna escritura quedo
    # fuera de todo repo git. Si todo vivia en repos, la reversibilidad
    # ya fue la autorizacion — los gates de evidencia siguen aplicando.
    _outside = [
        p for p in (changed_paths or [])
        if not _resolve_write_path(gate, p)
        or not _is_in_git_repo(_resolve_write_path(gate, p))
    ]

    if not gate["scope_done"] and _outside:
        return {
            "action": "continue",
            "message": (
                "BLOQUEADO: escribiste codigo pero no aprobaste scope. "
                "Llama analyze_scope(impact, modules, tests_status) y vuelve."
            ),
        }

    # Gates basados en EVIDENCIA (output crudo), no en booleans flipables.
    from .evidence import (
        evaluate_test_evidence,
        evaluate_lint_evidence,
        evaluate_typecheck_evidence,
    )

    test_ev = evaluate_test_evidence(gate.get("last_test_output"))
    if test_ev.verdict == "fail":
        return {"action": "continue", "message": f"BLOQUEADO: {test_ev.reason}"}
    if test_ev.verdict == "fail_open":
        logger.warning("arnes-gates: test evidence fail_open — %s", test_ev.reason)

    lint_ev = evaluate_lint_evidence(gate.get("last_lint_output"))
    if lint_ev.verdict == "fail":
        return {"action": "continue", "message": f"BLOQUEADO: {lint_ev.reason}"}
    if lint_ev.verdict == "fail_open":
        logger.warning("arnes-gates: lint evidence fail_open — %s", lint_ev.reason)

    typecheck_ev = evaluate_typecheck_evidence(gate.get("last_typecheck_output"))
    if typecheck_ev.verdict == "fail":
        return {"action": "continue", "message": f"BLOQUEADO: {typecheck_ev.reason}"}
    if typecheck_ev.verdict == "fail_open":
        logger.warning("arnes-gates: typecheck evidence fail_open — %s", typecheck_ev.reason)

    gate["done"] = True
    # F5.3: cierre limpio de tarea — señal para presenter_mode: on_finish.
    # El finish gate dejó cerrar con changed_paths y evidencia verde (o sin
    # ritual exigible): este turno ES el cierre que conviene vestir.
    gate["presenter_finish_clean"] = True
    return None  # todo verde: deja cerrar


def _on_post_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    **_kw: Any,
) -> None:
    """Hook post_tool_call: mantener indice incremental tras writes.

    Despues de un write_file/patch exitoso, reparsa solo ese archivo y
    actualiza el indice (no reconstruye todo). Tambien registra los paths
    escritos via terminal (cp, heredoc, sed -i, etc.) en written_paths.
    """
    args = args or {}

    # Fase 5: latch del presenter — toda tool ejecutada cuenta como "turno con
    # tools" para el routing (condicion d). Se resetea solo en pre_api_request
    # cuando cambia el turn_id (patron D7), nunca aca ni en transform.
    gate = gate_state.get()
    gate["presenter_tool_calls"] = gate.get("presenter_tool_calls", 0) + 1

    if tool_name in ("write_file", "patch"):
        path = args.get("path", "")
        if path:
            index_service.update_file(path)
            embedding_service.update_file(path)
            # F5.4: archivos del turno para el snapshot del vestidor.
            gate["presenter_turn_files"].add(path)
        return

    # F5.4: veredictos de verificación del turno — re-evaluar la evidencia
    # que los handlers ya guardaron (mismo código que el finish gate).
    if tool_name in ("run_tests", "run_lint", "run_typecheck"):
        _presenter_record_verdict(tool_name)
        return

    if tool_name == "terminal":
        cmd = args.get("command", "")
        if not cmd:
            return
        gate = gate_state.get()
        # F5.3: commit ejecutado con exito = cierre de tarea (el commit gate
        # solo deja pasar commits post-finish o con evidencia fresca). Senal
        # para presenter_mode: on_finish. Commits bloqueados (status=error)
        # no cuentan: no cerraron nada.
        if detect_git_commit(cmd) and _kw.get("status") != "error":
            gate["presenter_finish_clean"] = True
        # Trackear cd: actualizar terminal_cwd si el comando contiene cd.
        new_cwd = parse_cd(cmd, gate.get("terminal_cwd"))
        if new_cwd:
            gate["terminal_cwd"] = new_cwd
            logger.debug("arnes-gates: terminal_cwd actualizado: %s", new_cwd)
            # Construir el indice del nuevo proyecto si no existe ya.
            # Esto es lazy: solo se hace cuando el agente navega al proyecto.
            current_root = index_service.get_root()
            if current_root is None or str(current_root) != new_cwd:
                index_service.build_for(Path(new_cwd))
                embedding_service.build_for(Path(new_cwd))
                logger.info("arnes-gates: indice construido para %s", new_cwd)
        # Extraer paths de destino y registrarlos en written_paths.
        targets = extract_write_targets(cmd)
        for target in targets:
            gate["written_paths"].add(target)
            logger.debug("arnes-gates: terminal write registrado: %s", target)


def _on_pre_api_request(
    turn_id: str = "",
    user_message: str = "",
    **_kw: Any,
) -> None:
    """Hook pre_api_request: rearme humano del circuit breaker (Fix D7).

    Se dispara una vez por llamada al LLM. Cuando el turn_id cambia, el
    usuario escribió un mensaje nuevo — ya interrumpió el loop él mismo:
    insistir con el breaker solo secuestraría su turno. Rearmar acá
    (contador a 0, flag abajo) y dejar que el agente reintente con el
    diagnóstico/autorización que el usuario dio.
    """
    gate = gate_state.get()
    last = gate.get("breaker_turn_id")
    if turn_id and turn_id != last:
        gate["breaker_turn_id"] = turn_id
        # Fase 5: turno nuevo = latch del presenter en cero (patron D7).
        gate["presenter_tool_calls"] = 0
        # F5.3: el cierre limpio es una propiedad del TURNO, no acumulativa
        # (done sí lo es — sesion—; por eso este latch aparte).
        gate["presenter_finish_clean"] = False
        # F5.4: snapshot del turno tambien es por-turno.
        gate["presenter_turn_files"] = set()
        gate["presenter_turn_tests"] = None
        gate["presenter_turn_lint"] = None
        gate["presenter_turn_typecheck"] = None
        if gate.get("circuit_tripped") or gate.get("block_count"):
            gate["circuit_tripped"] = False
            gate["block_count"] = 0
            logger.info(
                "arnes-gates: circuit breaker rearmed (nuevo turno humano %s)",
                turn_id,
            )


# =============================================================================
# Registro del plugin.
# =============================================================================
def register(ctx) -> None:
    """Registro del plugin: hooks + tools.

    Usa las APIs reales de PluginContext:
      - ctx.register_hook(hook_name, callback)
      - ctx.register_tool(name, toolset, schema, handler, check_fn, emoji)
    """
    # Estado inicial limpio.
    gate_state.reset()

    # Hooks.
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_verify", _on_pre_verify)
    ctx.register_hook("pre_api_request", _on_pre_api_request)

    # Fase 5: presenter. El ctx queda disponible para ctx.llm / ctx.get_config
    # en los hooks del presenter (transform_llm_output + branch clarify).
    global _PRESENTER_CTX
    _PRESENTER_CTX = ctx
    ctx.register_hook("transform_llm_output", _on_transform_llm_output)

    # Tool analyze_scope.
    ctx.register_tool(
        name="analyze_scope",
        toolset="arnes-gates",
        schema=ANALYZE_SCOPE_SCHEMA,
        handler=_handle_analyze_scope,
        check_fn=None,  # siempre disponible
        emoji="🛡️",
    )

    # Tool find_references.
    ctx.register_tool(
        name="find_references",
        toolset="arnes-gates",
        schema=FIND_REFERENCES_SCHEMA,
        handler=_handle_find_references,
        check_fn=None,  # siempre disponible
        emoji="🔍",
    )

    # Tool search_semantic.
    ctx.register_tool(
        name="search_semantic",
        toolset="arnes-gates",
        schema=SEARCH_SEMANTIC_SCHEMA,
        handler=_handle_search_semantic,
        check_fn=None,
        emoji="🧭",
    )

    # Tool run_tests.
    ctx.register_tool(
        name="run_tests",
        toolset="arnes-gates",
        schema=RUN_TESTS_SCHEMA,
        handler=_handle_run_tests,
        check_fn=None,
        emoji="🧪",
    )

    # Tool run_lint.
    ctx.register_tool(
        name="run_lint",
        toolset="arnes-gates",
        schema=RUN_LINT_SCHEMA,
        handler=_handle_run_lint,
        check_fn=None,
        emoji="📐",
    )

    # Tool run_typecheck.
    ctx.register_tool(
        name="run_typecheck",
        toolset="arnes-gates",
        schema=RUN_TYPECHECK_SCHEMA,
        handler=_handle_run_typecheck,
        check_fn=None,
        emoji="🔤",
    )

    logger.info(
        "arnes-gates: plugin registrado "
        "(6 hooks + analyze_scope + find_references + search_semantic + "
        "run_tests + run_lint + run_typecheck)"
    )
