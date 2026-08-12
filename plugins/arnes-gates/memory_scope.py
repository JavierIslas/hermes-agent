"""Scoping de memoria: global + por proyecto (Decision 5b).

Detecta el proyecto activo por cwd y provee la info necesaria para que las
Fases 4-5 (cirugía de core) puedan:
  - Cargar memoria scoped al system prompt del worker.
  - Re-enrutar writes de memory_tool al archivo del proyecto.

En Fase 2 (plugin puro) solo implementamos detección + lectura. La inyección
al prompt y el re-enrutamiento de writes requieren tocar prompt_builder.py y
memory_tool.py (core surgery, Fases 4-5).

El modelo no decide dónde escribir — el plugin lo hace por cwd.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_active_project_slug: Optional[str] = None


def reset() -> None:
    global _active_project_slug
    _active_project_slug = None


def detect_project(cwd: Path | str | None = None) -> Optional[str]:
    """Detecta el proyecto activo por cwd y lo setea como activo.

    Precedencia:
      1. git-root del cwd (si es un repo).
      2. nombre del directorio base del cwd (fallback).
    Devuelve None si no hay proyecto detectable.

    NOTA: projects.db lookup queda para Fase 4 cuando podamos importar
    hermes_cli.projects_db sin acoplamiento prematuro.
    """
    global _active_project_slug
    cwd = Path(cwd) if cwd else Path.cwd()

    # 1. git-root
    slug = None
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            root = Path(result.stdout.strip())
            slug = root.name  # slug = nombre del repo
    except Exception:
        pass

    # 2. fallback: nombre del dir base
    if slug is None:
        slug = cwd.name or None

    _active_project_slug = slug
    return slug


def project_memory_dir(homes_dir: Path, slug: str) -> Path:
    """Path al directorio de memoria del proyecto."""
    return homes_dir / "projects" / slug


def project_memory_path(homes_dir: Path, slug: str) -> Path:
    """Path al MEMORY.md del proyecto."""
    return project_memory_dir(homes_dir, slug) / "MEMORY.md"


def load_scoped_memory(
    mem_dir: Path,
    cwd: Path | str | None = None,
) -> dict:
    """Carga memoria global + la del proyecto activo (si hay).

    Devuelve {"global": str, "project": str|None, "user": str,
              "project_slug": str|None}.

    El caller (Fase 4: prompt_builder) decide cómo inyectar esto en el
    system prompt del worker.
    """
    global _active_project_slug

    global_mem = _read_or_empty(mem_dir / "MEMORY.md")
    user_mem = _read_or_empty(mem_dir / "USER.md")

    slug = detect_project(cwd)  # ya setea _active_project_slug
    project_mem = None
    if slug:
        ppath = project_memory_path(mem_dir, slug)
        if ppath.exists():
            project_mem = ppath.read_text(encoding="utf-8")

    return {
        "global": global_mem,
        "user": user_mem,
        "project": project_mem,
        "project_slug": slug,
    }


def route_write(mem_dir: Path, content: str | None = None) -> Path:
    """Decide a qué archivo va el write de memoria.

    Si hay proyecto activo → projects/<slug>/MEMORY.md.
    Si no → global MEMORY.md.

    NOTA: en Fase 2 esto es informativo — el re-enrutamiento real del
    memory_tool requiere cirugía de core (Fase 4). Por ahora solo devuelve
    el path correcto para que tests y documentación lo usen.
    """
    if _active_project_slug:
        ppath = project_memory_path(mem_dir, _active_project_slug)
        ppath.parent.mkdir(parents=True, exist_ok=True)
        return ppath
    return mem_dir / "MEMORY.md"


def get_active_slug() -> Optional[str]:
    """Devuelve el slug del proyecto activo (None si no hay)."""
    return _active_project_slug


def _read_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
