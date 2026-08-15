"""Tools de retrieval: find_references (estructural) + search_semantic.

Port de refs/agent_juguete/tools/retrieval.py, adaptado al plugin arnes-gates:
- st._index / st._project_root → index_service.get_index() / get_root()
- st._manifest → no aplica (el plugin no tiene manifest; asume Python)
- st._embedder / st._embeddings → embedding_service (Task 2.4)
"""
from __future__ import annotations

import ast
import re
from typing import Any

from . import index_service
from .index import _iter_py_files, reverse_by_symbol


# =============================================================================
# find_references: dónde se define y dónde se usa un símbolo (estructural).
# =============================================================================
def _definiciones(symbol: str, index: dict[str, Any]) -> list[dict[str, Any]]:
    """Detalle de cada definición de symbol (del índice)."""
    out = []
    for path in reverse_by_symbol(index).get(symbol, []):
        entry = index["files"].get(path)
        if not entry:
            continue
        for sym in entry["symbols"]:
            if sym["name"] == symbol or sym["qualname"] == symbol:
                out.append(
                    {
                        "path": path,
                        "kind": sym["kind"],
                        "signature": sym["signature"],
                        "lineno": sym["lineno"],
                    }
                )
    return out


def _buscar_usos(symbol: str, root) -> dict[str, list[int]]:
    """Archivos donde symbol aparece como USO (no definición).

    Usa el adapter de cada archivo. Para Python: ast.Name/Attribute.
    Para lenguajes sin adapter: se saltea (fail-open).
    """
    from pathlib import Path
    from .lang_adapter import get_adapter_for_path

    root = Path(root)
    usos = {}
    for p in _iter_py_files(root):
        adapter = get_adapter_for_path(p)
        if adapter is None:
            continue  # sin adapter para este lenguaje
        try:
            source = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        nombres = adapter.extract_usages(source)
        if symbol in nombres:
            # Para Python podemos dar líneas exactas via AST. Para otros
            # lenguajes, sin info de línea: registramos el archivo completo.
            if adapter.name == "python":
                try:
                    tree = ast.parse(source, filename=str(p))
                except SyntaxError:
                    continue
                lineas = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Name) and node.id == symbol:
                        lineas.add(node.lineno)
                    elif isinstance(node, ast.Attribute) and node.attr == symbol:
                        lineas.add(node.lineno)
                if lineas:
                    usos[str(p.relative_to(root))] = sorted(lineas)
            else:
                usos[str(p.relative_to(root))] = [0]
    return usos


def find_references(symbol: str) -> str:
    """Retrieval estructural: dónde se define y dónde se usa un símbolo.

    Consulta el ÍNDICE del codebase (no grep textual): distingue el símbolo de
    texto igual en comentarios/strings, y responde 'dónde está definido y usado'
    en una sola mirada. Es el complemento estructural de grep.

    Requiere el índice cargado (on_session_start lo pobla); si no lo hay, AVISA.
    """
    index = index_service.get_index()
    root = index_service.get_root()

    if index is None or root is None:
        return (
            "AVISO: no hay índice del codebase cargado (on_session_start no lo "
            "construyó todavía). Mientras, usá search_files para buscar el "
            "símbolo como texto."
        )

    defs = _definiciones(symbol, index)
    usos = _buscar_usos(symbol, root)

    if not defs and not usos:
        return (
            f"No se encontró '{symbol}' ni como definición ni como uso en el "
            f"codebase. ¿Está bien escrito? Es case-sensitive."
        )

    lineas = []
    if defs:
        lineas.append(f"Definiciones ({len(defs)}):")
        for d in defs:
            lineas.append(
                f"  - {d['path']}: {d['kind']}  {d['signature']}  [línea {d['lineno']}]"
            )
    else:
        lineas.append("Definiciones: ninguna (no se define en el codebase indexado).")

    if usos:
        lineas.append(f"Usos ({len(usos)} archivos):")
        for path, lns in sorted(usos.items()):
            lineas.append(f"  - {path}: líneas {', '.join(str(n) for n in lns)}")
    else:
        lineas.append("Usos: ninguno fuera de la definición.")

    return f"find_references '{symbol}':\n" + "\n".join(lineas)


# =============================================================================
# search_semantic: retrieval por similitud léxica (TF-IDF).
# =============================================================================
_UMBRAL_MIN_SEMANTIC = 0.1


def search_semantic(query: str, top_k: int = 8) -> str:
    """Retrieval por similitud semántica: rankea los símbolos del codebase por
    cercanía a `query` (coseno de embeddings).

    Con el embedder TF-IDF default es solapamiento LÉXICO (no semántica real):
    útil para localizar por identifiers compartidos y rankear. Requiere el
    store de embeddings cargado (on_session_start lo pobla); si no lo hay,
    AVISA y deriva a find_references/grep.
    """
    from .embedding import similitud_coseno
    from . import embedding_service

    if not isinstance(query, str) or not query.strip():
        return "ERROR: query debe ser un string no vacío."

    store = embedding_service.get_store()
    embedder = embedding_service.get_embedder()
    if store is None or embedder is None:
        return (
            "AVISO: no hay store de embeddings cargado (search_semantic requiere "
            "embeddings). Usá find_references o search_files para localizar por "
            "texto/estructura."
        )

    items = store.get("items", {})
    if not items:
        return "AVISO: el store de embeddings está vacío (sin símbolos indexados todavía)."

    vec_query = embedder([query])[0]
    candidatos = [
        (similitud_coseno(vec_query, item["vector"]), item) for item in items.values()
    ]
    top = sorted(
        (p for p in candidatos if p[0] >= _UMBRAL_MIN_SEMANTIC),
        key=lambda x: x[0],
        reverse=True,
    )[:top_k]

    if not top:
        return (
            f"search_semantic '{query}': sin matches sobre el umbral "
            f"({_UMBRAL_MIN_SEMANTIC}). Probá con otras palabras o usá find_references."
        )

    lineas = [f"search_semantic '{query}' (top {len(top)}):"]
    for score, item in top:
        lineas.append(
            f"  - {item['path']}: {item['kind']} {item['signature']}  "
            f"[línea {item['lineno']}]  sim={score:.2f}"
        )
    return "\n".join(lineas)
