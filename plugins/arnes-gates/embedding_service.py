"""Servicio de embeddings: build/load/cache/maintain del store semántico.

Análogo a index_service pero para el store de embeddings (TF-IDF por defecto).
Se construye al arrancar (on_session_start) y se mantiene incremental via
update_file tras cada write_file/patch.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .embedding import EmbedderTFIDF, firma_de

logger = logging.getLogger(__name__)

# Estado del store (modulo-global, reseteado por sesion)
_store: dict[str, Any] | None = None
_embedder: Any = None


def reset() -> None:
    """Resetea el estado del store (llamar en on_session_start)."""
    global _store, _embedder
    _store = None
    _embedder = None


def build_for() -> dict[str, Any] | None:
    """Construye (o carga de cache + reconcilia) el store de embeddings.

    Depende del indice estructural (index_service) ya construido.
    """
    global _store, _embedder
    from . import index_service
    from .embeddings_store import load_or_build_embeddings

    index = index_service.get_index()
    root = index_service.get_root()
    if index is None or root is None:
        logger.debug("arnes-gates: no hay indice para construir embeddings")
        return None

    _embedder = EmbedderTFIDF()
    try:
        _store = load_or_build_embeddings(index, root, _embedder)
        n_items = len(_store.get("items", {})) if _store else 0
        logger.info(
            "arnes-gates: store de embeddings construido (%d items)", n_items
        )
        return _store
    except Exception as exc:
        logger.warning("arnes-gates: fallo construir embeddings: %s", exc)
        _store = None
        return None


def get_store() -> dict[str, Any] | None:
    return _store


def get_embedder() -> Any:
    return _embedder


def update_file(abs_path: str | Path) -> None:
    """Actualiza incremental el store tras un write_file/patch."""
    if _store is None or _embedder is None:
        return
    from . import index_service
    from .embeddings_store import update_embeddings, save_embeddings

    index = index_service.get_index()
    root = index_service.get_root()
    if index is None or root is None:
        return
    try:
        update_embeddings(_store, index, abs_path, root, _embedder)
        save_embeddings(root, _store)
    except Exception as exc:
        logger.debug("arnes-gates: update_embeddings fallo: %s", exc)
