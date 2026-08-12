"""Servicio del indice estructural: build/load/cache/maintain.

Envuelve index.py para que el resto del plugin (hooks, tools) no toque el
modulo del indice directamente. El indice se construye al arrancar la sesion
(on_session_start) sobre el cwd del proyecto, se cachea en disco, y se
mantiene incremental via update_file tras cada write_file/patch exitoso.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Estado del indice (modulo-global, reseteado por sesion)
_index: dict[str, Any] | None = None
_project_root: Path | None = None


def reset() -> None:
    """Resetea el estado del indice (llamar en on_session_start)."""
    global _index, _project_root
    _index = None
    _project_root = None


def build_for(project_root: Path) -> dict[str, Any] | None:
    """Construye (o carga de cache + reconcilia) el indice del proyecto.

    Devuelve el indice o None si la construccion falla.
    """
    global _index, _project_root
    _project_root = Path(project_root)
    try:
        from .index import load_or_build

        _index = load_or_build(_project_root)
        n_files = len(_index.get("files", {})) if _index else 0
        logger.info(
            "arnes-gates: indice construido para %s (%d archivos)",
            _project_root,
            n_files,
        )
        return _index
    except Exception as exc:
        logger.warning("arnes-gates: fallo construir indice: %s", exc)
        _index = None
        return None


def get_index() -> dict[str, Any] | None:
    return _index


def get_root() -> Path | None:
    return _project_root


def update_file(abs_path: str | Path) -> None:
    """Actualiza incremental el indice tras un write_file/patch exitoso.

    Solo reparsa el archivo modificado, no reconstruye todo.
    """
    if _index is None or _project_root is None:
        return
    try:
        from .index import update_file as _update_file, save_index

        _update_file(_index, abs_path, _project_root)
        save_index(_project_root, _index)
    except Exception as exc:
        logger.debug("arnes-gates: update_file fallo para %s: %s", abs_path, exc)


def build_for_cwd() -> dict[str, Any] | None:
    """Construye el indice para el cwd actual (helper para on_session_start)."""
    cwd = Path(os.getcwd())
    return build_for(cwd)
