"""Estado de gates del arnes (por sesion, en memoria).

Adaptado de refs/agent_juguete/state.py. Es la hoja del grafo de imports
del plugin: nadie importa de aca excepto __init__ y gates, que leen/escriben
este estado. Se resetea en on_session_start.
"""
from __future__ import annotations

from typing import Any


def _new_state() -> dict[str, Any]:
    return {
        # Gates principales
        "scope_done": False,       # prendido por analyze_scope tras validar el plan
        "done": False,             # prendido por finish tras tests+lint+typecheck verdes
        "last_plan": None,         # ultimo plan aprobado por analyze_scope
        # Tracking de paths (para read_before_write)
        "read_paths": set(),       # archivos leidos en este turno (se recalcula en compactacion)
        "ever_read": set(),        # acumulativo: leido alguna vez (no se compacta)
        "written_paths": set(),    # archivos escritos en esta sesion
        # Estado de verificacion
        "tests_green": False,
        "lint_green": False,
        "typecheck_green": False,
        "verify_fail_open": False,  # True si run_tests no pudo detectar el proyecto (cwd=/opt/hermes)
        "terminal_cwd": None,      # cwd del terminal (trackeado via cd). None = usar Path.cwd()
        "block_count": 0,          # bloqueos consecutivos sin progreso (circuit breaker)
        "circuit_tripped": False,  # True cuando el circuit breaker activó (bloquea todo)
        "breaker_turn_id": None,   # turn_id visto en el último pre_api_request (Fix D7: rearme humano)
        # Fase 5: presenter (vestir la entrega sin contaminar al worker)
        "presenter_tool_calls": 0,  # latch: tools ejecutadas en el turno (reset por turn_id nuevo, patron D7)
        "presenter_turn_id": None,  # turn_id del último pre_api_request visto por el latch
        "presenter_dressed_count": 0,  # telemetría: entregas vestidas en la sesión
        # F5.3 (2026-08-21): on_finish — viste SOLO el turno que cierra la tarea
        "presenter_finish_clean": False,  # latch: este turno cerró limpio (finish gate OK o commit)
        # F5.4: snapshot del turno — archivos escritos y veredictos de ESTE turno
        "presenter_turn_files": set(),  # paths escritos en el turno (reset por turn_id nuevo, patron D7)
        "presenter_turn_tests": None,   # resumen del veredicto de tests del turno
        "presenter_turn_lint": None,    # "limpio" | resumen del veredicto de lint
        "presenter_turn_typecheck": None,
        # F5.3: telemetría de fail-open (dogfood D13: 2/3 vestidos morían en
        # timeout sin que nadie lo supiera — el presenter era teatro muerto)
        "presenter_failopen_integrity": 0,
        "presenter_failopen_llm": 0,
        "presenter_failopen_timeout": 0,
        "presenter_failopen_empty": 0,
        # Modo arranque (repo sin codigo previo)
        "modo_arranque": False,
        "spec_done": False,
        "spec": None,
        # Outputs crudos (para el juez / debugging + evidence-based gates)
        "last_test_output": None,
        "last_lint_output": None,
        "last_typecheck_output": None,
        "last_judge": None,
        "judge_telemetry": [],
        "ask_human_telemetry": [],
    }


_state: dict[str, Any] = _new_state()


def reset() -> None:
    """Resetea el estado a valores iniciales (llamar en on_session_start)."""
    global _state
    _state = _new_state()


def get() -> dict[str, Any]:
    """Devuelve el dict de estado mutable (lectura/escritura directa)."""
    return _state
