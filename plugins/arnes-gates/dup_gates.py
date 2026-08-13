"""Gate Q3: duplicación estructural — struct_hash.

Bloquea si el código escrito duplica estructuralmente una def existente en el
índice (mismo cuerpo normalizado, mismo SHA256 de AST).

Compara el struct_hash de cada def del código escrito contra todos los
struct_hashes del índice. Si hay match, bloquea diciendo "reutilizá la
existente".

Excluye:
  - defs citadas en reuse (reuso legítimo, cubierto por Q1/Q2).
  - la def del propio archivo (reescribir tu def no es duplicación).
  - defs triviales (< MIN_NODOS_DUP nodos en el AST).

Fail-open: sin índice cargado → no bloquea.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .index import _buscar_duplicados


def validar_duplicacion(
    content: str,
    rel_path: str,
) -> Optional[str]:
    """Bloquea si el código duplica estructuralmente una def existente.

    Devuelve None si OK (o si no aplica). Devuelve BLOQUEADO si hay duplicación.

    rel_path: path relativo al project_root (para excluir la propia def).
    """
    from . import index_service, state as gate_state

    index = index_service.get_index()
    if index is None:
        return None  # fail-open: sin índice no se puede verificar.

    # Nombres citados en reuse (excluidos — reuso legítimo).
    plan = gate_state.get().get("last_plan") or {}
    excluir = frozenset(
        str(it.get("name", "")).split(".")[-1]
        for it in plan.get("reuse") or []
        if isinstance(it, dict)
    )

    dups = _buscar_duplicados(content, index, rel_path, excluir)
    if not dups:
        return None

    nuevo, qual, path_ex, _sig = dups[0]
    return (
        f"BLOQUEADO: '{nuevo}' duplica estructuralmente a '{qual}' ya definida "
        f"en {path_ex} (mismo cuerpo normalizado). Reutilizá la existente: "
        f"importala y llamala en vez de reimplementarla."
    )
