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

    reuse (opcional): abstracciones existentes a reutilizar. Se verifica contra
    el indice estructural: cada name debe existir y su firma (params, returns)
    debe calzar con la definicion real. Si no hay indice cargado, se acepta el
    reuse pero no se verifica (fail-open).
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

    # Validar estructura de reuse.
    if reuse:
        for i, item in enumerate(reuse):
            if not isinstance(item, dict) or "name" not in item:
                return (
                    f"ERROR: reuse[{i}] debe ser un objeto con 'name'. "
                    f"Recibido: {item!r}"
                )

    # Validar reuse contra el indice estructural (si hay indice cargado).
    error_reuse = _validar_reuse(reuse)
    if error_reuse is not None:
        return error_reuse

    plan = {
        "impact": impact,
        "modules": modules,
        "tests_status": tests_status,
        "reuse": reuse or [],
    }
    gate_state.get()["scope_done"] = True
    gate_state.get()["last_plan"] = plan
    return f"OK: scope aprobado. Plan={plan}. Ya podes escribir codigo."


def _validar_reuse(reuse: list[dict[str, Any]] | None) -> str | None:
    """Verifica cada abstraccion citada contra el indice real.

    None si todo OK (o si no aplica). String de ERROR accionable si falla.

    - reuse vacio → None (no hay nada que verificar).
    - Sin indice cargado → None (fail-open: no se puede verificar).
    - Simbolo inexistente → ERROR.
    - Firma que no calza → ERROR mostrando citada vs real.
    """
    if not reuse:
        return None

    from . import index_service
    index = index_service.get_index()
    if index is None:
        # Sin indice: fail-open. No se puede verificar pero no se bloquea.
        return None

    from .index import extract_signature, signatures_match

    for cited in reuse:
        name = cited["name"]
        candidatos = extract_signature(name, index)
        if not candidatos:
            return (
                f"ERROR: reuse '{name}' no existe en el indice. No citaste "
                "una abstraccion real. Localizala con find_references y "
                "releela antes de reutilizarla."
            )
        if not any(signatures_match(cited, c) for c in candidatos):
            return (
                f"ERROR: la firma citada para '{name}' no calza con la real.\n"
                f"  citada: {_render_cited(cited)}\n"
                f"  real:   {_render_real(candidatos[0])}\n"
                "Relee la definicion con find_references/read_file."
            )
    return None


def _render_cited(cited: dict[str, Any]) -> str:
    """Firma citada legible para el mensaje de ERROR."""
    params = ", ".join(
        p["name"] + (f": {p['type']}" if p.get("type") else "")
        for p in cited.get("params") or []
    )
    ret = f" -> {cited['returns']}" if cited.get("returns") else ""
    return f"{cited['name']}({params}){ret}"


def _render_real(sym: dict[str, Any]) -> str:
    """Firma real (del indice) legible para el mensaje de ERROR."""
    params = ", ".join(
        p["name"] + (f": {p['type']}" if p["type"] else "")
        for p in sym.get("params") or []
    )
    ret = f" -> {sym['returns']}" if sym.get("returns") else ""
    return f"{sym['qualname']}({params}){ret}"
