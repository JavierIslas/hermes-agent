"""Viewer del par worker/presenter — rutas API del dashboard plugin.

Montado en /api/plugins/arnes-viewer/ por el sistema de dashboard plugins.
HERMES_HOME/presenter_pairs.jsonl es el registro que escribe el hook
transform_llm_output (F5.8): una línea JSON por turno vestido o fail-open.

Capa intencionalmente fina: lee el JSONL y nada más. Sin DB, sin estado,
sin escrituras — es debug de lectura.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, List

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_ENTRIES = 200  # el visor muestra las últimas 200; el JSONL retiene 500


def _pairs_path() -> os.PathLike:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "presenter_pairs.jsonl"


def _load_entries() -> List[dict]:
    """Lee el JSONL, última línea primero. Archivo ausente = lista vacía."""
    try:
        path = _pairs_path()
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        entries: List[dict] = []
        for ln in lines:
            try:
                entries.append(json.loads(ln))
            except json.JSONDecodeError:
                # línea parcial (corte de proceso a mitad de write): skip
                logger.debug("viewer: línea JSONL corrupta salteada")
        entries.reverse()  # más reciente primero
        return entries[:_MAX_ENTRIES]
    except Exception:
        logger.exception("viewer: no se pudo leer presenter_pairs.jsonl")
        return []


@router.get("/pairs")
async def get_pairs() -> dict:
    """Turnos del par: raw, dressed, outcome, user_message, tool_calls."""
    return {"entries": _load_entries()}


@router.get("/stats")
async def get_stats() -> dict:
    """Contadores de outcome para el header del visor."""
    entries = _load_entries()
    counts: dict[str, int] = {}
    for e in entries:
        outcome = str(e.get("outcome", "?"))
        counts[outcome] = counts.get(outcome, 0) + 1
    return {"total": len(entries), "outcomes": counts}
