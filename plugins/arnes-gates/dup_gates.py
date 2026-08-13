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


# =============================================================================
# Gate Q4: duplicación SEMÁNTICA (coseno de embeddings) — fail-open.
# =============================================================================
UMBRAL_DUP_SEMANTICA = 0.85


def validar_duplicacion_semantica(
    content: str,
    rel_path: str,
) -> Optional[str]:
    """Gate de duplicación SEMÁNTICA (capa bonus sobre la estructural).

    Compara cada def del content (no trivial, no citada en reuse) contra los
    símbolos del store de embeddings EXCLUYENDO el propio archivo y el reuse,
    por coseno. Si alguna pareja supera UMBRAL_DUP_SEMANTICA → BLOQUEADO.

    Es la red bajo lo que el detector estructural deja pasar: misma forma
    atrapa el estructural; MISMO SIGNIFICADO con forma DISTINTA (reordenados,
    renombrados, logging extra) lo atrapa acá.

    Fail-open: sin embedder/store, store vacío, content que no parsea, o
    cualquier excepción → None (no bloquea).
    """
    from . import embedding_service, state as gate_state
    from .embedding import similitud_coseno
    from .index import (
        _collect_def_nodes,
        _normalizar_def,
        MIN_NODOS_DUP,
        slice_source,
    )

    embedder = embedding_service.get_embedder()
    store = embedding_service.get_store()
    if embedder is None or store is None:
        return None

    items = store.get("items", {})
    if not items:
        return None

    import ast

    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None

    # Nombres a excluir (reuse + propio archivo).
    plan = gate_state.get().get("last_plan") or {}
    excluir_nombres = {
        str(it.get("name", "")).split(".")[-1]
        for it in plan.get("reuse") or []
        if isinstance(it, dict)
    }

    # Candidatos del store: excluir propio archivo y reuse.
    candidatos = [
        it
        for it in items.values()
        if it.get("path") != rel_path
        and it.get("qualname", "").split(".")[-1] not in excluir_nombres
    ]
    if not candidatos:
        return None

    # Extraer defs nuevas del content (no triviales, no excluidas).
    try:
        nuevas = []
        for def_node in _collect_def_nodes(tree):
            nombre = def_node.name.split(".")[-1]
            if nombre in excluir_nombres:
                continue
            _canonical, ncount = _normalizar_def(def_node)
            if ncount < MIN_NODOS_DUP:
                continue
            nuevas.append(
                (nombre, slice_source(content, def_node.lineno, def_node.end_lineno))
            )
        if not nuevas:
            return None
        vectores = embedder([chunk for _, chunk in nuevas])
    except Exception:
        return None

    # Comparar cada def nueva contra todos los candidatos.
    bloques = []
    for (nombre, _chunk), vec in zip(nuevas, vectores, strict=True):
        mejor_score, mejor_item = _mejor_candidato(vec, candidatos)
        if mejor_item is not None and mejor_score >= UMBRAL_DUP_SEMANTICA:
            bloques.append((nombre, mejor_item, mejor_score))

    if not bloques:
        return None

    detalle = "\n".join(
        f"  - '{nombre}' duplica semánticamente a '{item['qualname']}' "
        f"en {item['path']} (sim={score:.2f})"
        for nombre, item, score in bloques
    )
    return (
        "BLOQUEADO: duplicación semántica — reutilizá en vez de reimplementar. "
        "(El detector estructural no la atrapó porque la FORMA difiere, pero "
        "el SIGNIFICADO coincide.)\n" + detalle
    )


def _mejor_candidato(
    vec: list[float], candidatos: list[dict[str, Any]]
) -> tuple[float, Optional[dict[str, Any]]]:
    """(score, item) del candidato más cercano a vec, o (-1.0, None)."""
    from .embedding import similitud_coseno

    mejor_score = -1.0
    mejor_item = None
    for it in candidatos:
        score = similitud_coseno(vec, it["vector"])
        if score > mejor_score:
            mejor_score = score
            mejor_item = it
    return mejor_score, mejor_item
