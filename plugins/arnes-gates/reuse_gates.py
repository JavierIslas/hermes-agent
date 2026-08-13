"""Gate Q2: validar_reuse_en_diff — uso real del reuse en el código escrito.

Verifica que los símbolos citados en last_plan['reuse'] aparecen como USO
(ast.Name/ast.Attribute) en el código que se está escribiendo, más los archivos
ya escritos en la sesión (diff acumulado).

El gate Q1 valida que la firma citada CALZA con la real. Este gate valida que
el agente REALMENTE USA lo que citó. Un agente puede citar correctamente una
abstracción (pasa Q1) y después no usarla (falla Q2).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .index import _nombres_referenciados


def validar_reuse_en_diff(
    content: str,
    written_paths: set[str],
) -> Optional[str]:
    """Verifica que cada símbolo citado en reuse aparece como USO en el código.

    Devuelve None si todo OK (o si no aplica). Devuelve un mensaje de
    BLOQUEADO si falta algún símbolo citado en reuse.

    - reuse vacío o ausente → None (no hay nada que verificar).
    - El content actual + los archivos ya escritos forman el "diff acumulado".
    - Un símbolo en un comentario o string NO cuenta como uso (ast.Name solo).
    """
    from . import state as gate_state

    plan = gate_state.get().get("last_plan") or {}
    reuse = plan.get("reuse") or []
    if not reuse:
        return None

    # Recopilar todos los nombres referenciados en el content actual.
    nombres = _nombres_referenciados(content)

    # Sumar los nombres de los archivos ya escritos en la sesión.
    for path_str in written_paths:
        try:
            otros = Path(path_str).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        nombres |= _nombres_referenciados(otros)

    # Verificar que cada símbolo citado en reuse aparece como uso.
    faltantes = []
    for item in reuse:
        nombre = str(item.get("name", "")).split(".")[-1]
        if nombre and nombre not in nombres:
            faltantes.append(nombre)

    if faltantes:
        return (
            f"BLOQUEADO: el plan se compromete a reutilizar {faltantes} pero "
            f"no aparecen como USO en el código escrito (se verificó el "
            f"archivo actual + los ya escritos en la sesión). Reutilizalos "
            f"de verdad — llamalos o importalos, no los menciones en "
            f"comentarios. Si los vas a usar en otro archivo, escribi ese "
            f"archivo antes o junto con este."
        )
    return None
