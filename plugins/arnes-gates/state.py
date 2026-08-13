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
        # Modo arranque (repo sin codigo previo)
        "modo_arranque": False,
        "spec_done": False,
        "spec": None,
        # Outputs crudos (para el juez / debugging)
        "last_test_output": None,
        "last_lint_output": None,
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
