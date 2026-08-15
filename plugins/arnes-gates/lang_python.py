"""Python language adapter — envuelve el parser ast existente del index.py.

Este adapter delega a las funciones internas de index.py (_parse_source,
_collect_def_nodes, _normalizar_def, _nombres_referenciados, etc.) que YA
están construidas y testeadas. No reescribe nada: expone el contrato de
LanguageAdapter sobre la implementación existente.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .lang_adapter import LanguageAdapter


class PythonAdapter(LanguageAdapter):
    """Adapter para Python via stdlib ast."""

    @property
    def name(self) -> str:
        return "python"

    @property
    def extensions(self) -> frozenset[str]:
        return frozenset({".py"})

    def parse_source(self, source: str, rel: str) -> Optional[dict[str, Any]]:
        """Delega a index._parse_source."""
        from .index import _parse_source
        return _parse_source(source, rel, Path(rel).parent)

    def check_syntax(self, source: str, path: str) -> Optional[str]:
        """Verifica sintaxis Python via compile()."""
        try:
            compile(source, path, "exec")
            return None
        except SyntaxError as exc:
            return f"error de sintaxis Python (línea {exc.lineno}: {exc.msg})"

    def extract_usages(self, content: str) -> set[str]:
        """Delega a index._nombres_referenciados."""
        from .index import _nombres_referenciados
        return _nombres_referenciados(content)
