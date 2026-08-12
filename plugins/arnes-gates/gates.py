"""Gates del arnes: analyze_scope (valida el plan).

Port de refs/agent_juguete/tools/gates.py:analyze_scope, adaptado para escribir
en gate_state del plugin (no en st.state del agent_juguete).

En Fase 1 solo se porta analyze_scope (validacion de campos basicos del plan).
La verificacion de reuse (que requiere indice estructural) viene en Fase 2.
"""
from __future__ import annotations

from typing import Any

from . import state as gate_state

VALID_IMPACT = {"BAJO", "MEDIO", "ALTO"}
VALID_TESTS = {"covered", "partial", "none"}


def analyze_scope(
    impact: str,
    modules: list[str],
    tests_status: str,
    reuse: list[dict[str, Any]] | None = None,
) -> str:
    """Gate de CALIDAD sobre el plan, con valores estructurados (no prosa).

    Recibe campos tipados y los valida. Si todo calza, prende scope_done.
    Esto reemplaza al 'touch .scope-guard-done': el flag lo mueve ESTA funcion,
    y solo si la validacion paso. El modelo no puede moverlo por su cuenta.

    reuse (opcional): abstracciones existentes a reutilizar. En Fase 1 NO se
    verifica contra el indice (requiere Fase 2). Se acepta pero se ignora.
    """
    if impact not in VALID_IMPACT:
        return f"ERROR: impact='{impact}' invalido. Debe ser {sorted(VALID_IMPACT)}."
    if not modules or not isinstance(modules, list):
        return "ERROR: modules debe ser una lista no vacia."
    if tests_status not in VALID_TESTS:
        return f"ERROR: tests_status='{tests_status}' invalido. Debe ser {sorted(VALID_TESTS)}."
    if tests_status == "none":
        return (
            "ERROR: tests_status='none' exige TDD. Cambia tests_status a "
            "'partial' o 'covered' para indicar que vas a escribir tests "
            "primero, y vuelve a validar."
        )

    # TODO (Fase 2): validar reuse contra el indice estructural.
    # Por ahora solo validamos que sea una lista de dicts con "name".
    if reuse:
        for i, item in enumerate(reuse):
            if not isinstance(item, dict) or "name" not in item:
                return (
                    f"ERROR: reuse[{i}] debe ser un objeto con 'name'. "
                    f"Recibido: {item!r}"
                )

    plan = {
        "impact": impact,
        "modules": modules,
        "tests_status": tests_status,
        "reuse": reuse or [],
    }
    gate_state.get()["scope_done"] = True
    gate_state.get()["last_plan"] = plan
    return f"OK: scope aprobado. Plan={plan}. Ya podes escribir codigo."
