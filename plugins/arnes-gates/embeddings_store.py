"""Store de embeddings por símbolo, con content-addressing e invalidación por firma.

Vive APARTE del índice estructural (los vectores son grandes y el schema es distinto):
`<ZAI_INDEX_DIR>/<sha(root)>.embeddings.json`. Reusa `index._cache_dir` y el mismo
sha del root que `_cache_path`, así target -> mismo par de archivos.

Dos mecanismos de invalidación:
- Por HASH del chunk: si el cuerpo del símbolo no cambió, se reusa el vector (no se
  re-embebe — el embed es lo caro, sea modelo o red). Análogo a `index.reconcile`.
- Por FIRMA del embedder: cambiar de proveedor o de dimensión produce vectores
  incompatibles (otra longitud) -> el store se reconstruye desde cero.

El embedder se recibe por parámetro (DI); acá no se sabe si es TF-IDF u OpenAI.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from .embedding import firma_de
from .index import _cache_dir, chunk_de_symbol

EMBED_VERSION = 1


def _embeddings_path(root: str | Path) -> Path:
    """Path del store para `root`: <cache_dir>/<sha(root resolved)>.embeddings.json.

    Mismo digest que `index._cache_path` (sha del root absoluto): mismo target ->
    mismo par (índice + embeddings).
    """
    digest = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()
    cache = _cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    return cache / f"{digest}.embeddings.json"


def _hash_chunk(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def _chunk_id(rel: str, qualname: str) -> str:
    return f"{rel}::{qualname}"


def _item(sym: dict[str, Any], texto: str, rel: str, vector: list[float]) -> dict[str, Any]:
    return {
        "hash": _hash_chunk(texto),
        "vector": list(vector),
        "path": rel,
        "qualname": sym["qualname"],
        "kind": sym["kind"],
        "signature": sym["signature"],
        "lineno": sym["lineno"],
    }


def _leer_source(root: str | Path, rel: str) -> str | None:
    try:
        return (Path(root) / rel).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _chunks_actuales(
    index: dict[str, Any], root: str | Path
) -> Iterator[tuple[str, dict[str, Any], str, str]]:
    """Itera (cid, sym, texto, rel) para todos los símbolos del índice, leyendo disco."""
    for rel, entry in index["files"].items():
        source = _leer_source(root, rel)
        if source is None:
            continue
        for sym in entry.get("symbols", []):
            yield _chunk_id(rel, sym["qualname"]), sym, chunk_de_symbol(sym, source), rel


def build_embeddings(
    index: dict[str, Any], root: str | Path, embedder: Any
) -> dict[str, Any]:
    """Embebe todos los símbolos del índice desde cero (un lote por llamada)."""
    items = {}
    pendientes = list(_chunks_actuales(index, root))
    if pendientes:
        vectores = embedder([t for _, _, t, _ in pendientes])
        for (cid, sym, texto, rel), vec in zip(pendientes, vectores, strict=True):
            items[cid] = _item(sym, texto, rel, vec)
    return {"version": EMBED_VERSION, "firma": firma_de(embedder), "items": items}


def reconcile_embeddings(
    store: dict[str, Any], index: dict[str, Any], root: str | Path, embedder: Any
) -> dict[str, Any]:
    """Solo re-embebe los símbolos cuyo chunk cambió (o son nuevos); reusa el resto.

    Si la firma del store no calza con la del embedder (otro proveedor/dim), reconstruye
    desde cero: vectores de otra longitud no son comparables.
    """
    if store.get("firma") != firma_de(embedder):
        return build_embeddings(index, root, embedder)

    items = store["items"]
    actuales = {cid: (sym, texto, rel) for cid, sym, texto, rel in _chunks_actuales(index, root)}

    # Borrar items de símbolos que ya no existen.
    for cid in list(items):
        if cid not in actuales:
            items.pop(cid)

    # Reusar los que no cambiaron (hash igual) — actualizando metadata por si movió.
    pendientes = []
    for cid, (sym, texto, rel) in actuales.items():
        existente = items.get(cid)
        if existente is not None and existente.get("hash") == _hash_chunk(texto):
            existente["lineno"] = sym["lineno"]
            existente["signature"] = sym["signature"]
            existente["kind"] = sym["kind"]
        else:
            pendientes.append((cid, sym, texto, rel))

    if pendientes:
        vectores = embedder([t for _, _, t, _ in pendientes])
        for (cid, sym, texto, rel), vec in zip(pendientes, vectores, strict=True):
            items[cid] = _item(sym, texto, rel, vec)
    return store


def update_embeddings(
    store: dict[str, Any],
    index: dict[str, Any],
    abs_path: str | Path,
    root: str | Path,
    embedder: Any,
) -> dict[str, Any]:
    """Re-chunkea + re-embebe UN archivo (post-write_file), reemplazando sus items."""
    rel = str(Path(abs_path).relative_to(Path(root)))
    for cid in [c for c, it in store["items"].items() if it["path"] == rel]:
        store["items"].pop(cid)

    entry = index["files"].get(rel)
    if entry is None:
        return store  # archivo sin símbolos / no parseable
    source = _leer_source(root, rel)
    if source is None:
        return store

    pendientes = [
        (_chunk_id(rel, sym["qualname"]), sym, chunk_de_symbol(sym, source))
        for sym in entry.get("symbols", [])
    ]
    if not pendientes:
        return store
    vectores = embedder([t for _, _, t in pendientes])
    for (cid, sym, texto), vec in zip(pendientes, vectores, strict=True):
        store["items"][cid] = _item(sym, texto, rel, vec)
    return store


def _es_cargable_emb(store: dict[str, Any], embedder: Any) -> bool:
    return store.get("version") == EMBED_VERSION and store.get("firma") == firma_de(embedder)


def load_embeddings(root: str | Path) -> dict[str, Any] | None:
    path = _embeddings_path(root)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_embeddings(root: str | Path, store: dict[str, Any]) -> None:
    path = _embeddings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, sort_keys=True, ensure_ascii=False)


def load_or_build_embeddings(
    index: dict[str, Any], root: str | Path, embedder: Any
) -> dict[str, Any]:
    """Carga y reconcilia el store; si no hay (o la firma cambió), lo construye."""
    store = load_embeddings(root)
    if store is None or not _es_cargable_emb(store, embedder):
        return build_embeddings(index, root, embedder)
    return reconcile_embeddings(store, index, root, embedder)
