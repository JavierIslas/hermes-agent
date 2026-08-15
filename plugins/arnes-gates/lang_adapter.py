"""Language adapter registry — abstracción multi-lenguaje del índice estructural.

El índice (index.py) originalmente asumía Python (ast.parse) en toda la cadena.
Este módulo introduce un registry de adapters: cada adapter declara qué
extensiones maneja y cómo parsear código a un formato común.

Contrato del adapter (lo que el índice necesita):
  - extensions: set de sufijos que maneja (".py", ".js", etc.)
  - parse_source(source, rel) -> {module, symbols, imports} | None
  - check_syntax(source, path) -> str | None (error msg or None=OK)
  - normalize_for_dup(content) -> list[(name, canonical_str, ncount)]
      canonical para struct_hash. [] si el lenguaje no soporta detección dup.
  - extract_usages(content) -> set[str] (nombres referenciados para reuse_in_diff)

Si un lenguaje no tiene adapter:
  - Sus archivos no se indexan (fail-open, índice parcial).
  - Los gates estructurales (Q3/Q4) degradan a AVISO.
  - find_references no los ve.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional


class LanguageAdapter(ABC):
    """Contrato para un adapter de lenguaje."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Nombre del lenguaje ('python', 'javascript', etc.)."""

    @property
    @abstractmethod
    def extensions(self) -> frozenset[str]:
        """Sufijos de archivo que este adapter maneja ('.py', '.js')."""

    @abstractmethod
    def parse_source(self, source: str, rel: str) -> Optional[dict[str, Any]]:
        """Parsea código fuente → {module, symbols, imports} o None si falla.

        symbols: list[{name, kind, qualname, signature, lineno, end_lineno,
                       params, returns, struct_hash}]
        imports: list[dict] con info de imports del proyecto.
        module: str (nombre de módulo derivado del path).
        """

    @abstractmethod
    def check_syntax(self, source: str, path: str) -> Optional[str]:
        """Verifica sintaxis. Devuelve None si OK, o mensaje de error."""

    @abstractmethod
    def extract_usages(self, content: str) -> set[str]:
        """Nombres referenciados en el código (para reuse_in_diff)."""


# =============================================================================
# Registry.
# =============================================================================
_registry: dict[str, LanguageAdapter] = {}
_ext_map: dict[str, LanguageAdapter] = {}


def register(adapter: LanguageAdapter) -> None:
    """Registra un adapter."""
    _registry[adapter.name] = adapter
    for ext in adapter.extensions:
        _ext_map[ext] = adapter


def get_adapter_for_path(path: str | Path) -> Optional[LanguageAdapter]:
    """Devuelve el adapter para un path dado, o None si no hay."""
    return _ext_map.get(Path(path).suffix)


def all_extensions() -> frozenset[str]:
    """Todas las extensiones de todos los adapters registrados."""
    return frozenset(_ext_map.keys())


def get_adapter(name: str) -> Optional[LanguageAdapter]:
    """Devuelve el adapter por nombre."""
    return _registry.get(name)


def all_adapters() -> list[LanguageAdapter]:
    """Todos los adapters registrados."""
    return list(_registry.values())


# =============================================================================
# Auto-detección de stack desde el filesystem.
# =============================================================================
_STACK_MARKERS: list[tuple[str, str]] = [
    # (archivo marcador, nombre del stack)
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("setup.cfg", "python"),
    ("requirements.txt", "python"),
    ("Pipfile", "python"),
    ("package.json", "javascript"),
    ("tsconfig.json", "typescript"),
    ("go.mod", "go"),
    ("Cargo.toml", "rust"),
    ("composer.json", "php"),
    ("pom.xml", "java"),
    ("build.gradle", "java"),
    ("build.gradle.kts", "java"),
    ("Gemfile", "ruby"),
    ("mix.exs", "elixir"),
    ("CMakeLists.txt", "cpp"),
    ("Makefile", "c"),
]


def detect_stack(root: str | Path) -> list[str]:
    """Detecta el/los stack(s) del proyecto por archivos marcadores.

    Devuelve una lista de nombres de stack detectados (puede ser múltiple:
    un proyecto con pyproject.toml + package.json es python+javascript).
    [] si no detecta ninguno.
    """
    root = Path(root)
    stacks: list[str] = []
    for marker, stack in _STACK_MARKERS:
        if (root / marker).exists() and stack not in stacks:
            stacks.append(stack)
    # Si no hay marcadores pero hay .py → python por defecto.
    if not stacks:
        for p in root.rglob("*.py"):
            if not any(part in {"__pycache__", ".git", ".venv", "node_modules"} for part in p.parts):
                stacks.append("python")
                break
    return stacks


def adapters_for_root(root: str | Path) -> list[LanguageAdapter]:
    """Adapters aplicables a un proyecto dado su root.

    Usa detect_stack + los adapters registrados. Si un stack detectado no
    tiene adapter registrado, se omite (fail-open para ese lenguaje).
    """
    stacks = detect_stack(root)
    result: list[LanguageAdapter] = []
    for s in stacks:
        adapter = get_adapter(s)
        if adapter is not None and adapter not in result:
            result.append(adapter)
    # Si no se detectó ningún stack con adapter, devolver python como default.
    if not result:
        default = get_adapter("python")
        if default is not None:
            result.append(default)
    return result


# =============================================================================
# Registro automático del adapter Python (siempre disponible).
# =============================================================================
def _ensure_python_registered() -> None:
    """Registra el PythonAdapter si no está ya registrado."""
    if "python" not in _registry:
        from .lang_python import PythonAdapter
        register(PythonAdapter())


# Auto-registro al importar.
_ensure_python_registered()
