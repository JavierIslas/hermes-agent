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

logger = logging.getLogger(__name__)


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
    BLOCK_THRESHOLD = 5
    if gate.get("circuit_tripped"):
        return {
            "action": "block",
            "message": (
                "CIRCUIT BREAKER: el arnés detectó que estás stuck después de "
                f"{BLOCK_THRESHOLD} bloqueos consecutivos sin progreso. "
                "Explicale al usuario qué te bloquea y esperá su respuesta. "
                "No sigas intentando tools."
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

    # Write gate: write_file y patch requieren scope + read_before_write.
    if tool_name in ("write_file", "patch"):
        # Gate 1: scope_done
        if not gate["scope_done"]:
            return {
                "action": "block",
                "message": (
                    "BLOQUEADO: no hay scope aprobado. Llama analyze_scope "
                    "con un plan valido (impact, modules, tests_status) antes "
                    "de escribir codigo."
                ),
            }

        path = args.get("path", "")
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
                return {
                    "action": "block",
                    "message": (
                        "BLOQUEADO: no podés commitear sin cerrar con finish "
                        "primero (falta scope aprobado o verificación de tests). "
                        "Asegurate de que el finish gate haya pasado antes de "
                        "materializar el cambio en git."
                    ),
                }
            # done=True: verificar dangling imports si hay indice.
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
            if not gate["scope_done"]:
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
    """
    if not coding or not changed_paths:
        return None  # no es task de codigo: deja cerrar

    gate = gate_state.get()

    if not gate["scope_done"]:
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

    if tool_name in ("write_file", "patch"):
        path = args.get("path", "")
        if path:
            index_service.update_file(path)
            embedding_service.update_file(path)
        return

    if tool_name == "terminal":
        cmd = args.get("command", "")
        if not cmd:
            return
        gate = gate_state.get()
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
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_verify", _on_pre_verify)

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
        "(4 hooks + analyze_scope + find_references + search_semantic + "
        "run_tests + run_lint + run_typecheck)"
    )
