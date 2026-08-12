"""Embedders: vectorizan texto -> vector de floats, para retrieval semántico.

DI inyectable (patrón del juez): el loop recibe un embedder y lo consumen
`search_semantic` y el gate de duplicación semántica. Default sin red
(`EmbedderTFIDF`); opt-in OpenAI (`embedding_openai.py`) para semántica real.

Agujero honesto: TF-IDF es solapamiento LÉXICO (bag-of-words), no semántica.
Útil para localizar por identifiers compartidos y rankear; NO capta sinónimos ni
conceptos sin palabras compartidas. La semántica real exige embeddings de modelo
(opt-in openai). El circuito (chunk -> vector -> store -> coseno -> ranking ->
invalidación) se desencanta y valida igual con el fallback.
"""

import hashlib
import re
import unicodedata
from typing import Any

DIM = 512
_ID_TFIDF = "tfidf"

_PALABRA = re.compile(r"[a-z0-9]+")


def _sin_acentos(texto: str) -> str:
    # Normaliza a ASCII: "área"->"area", "círculo"->"circulo". Sin esto, el regex
    # [a-z0-9]+ parte los tokens en los acentos ("área"->"rea") y una query en
    # español nunca matchea código ASCII (bug cazado en la validación #24).
    return "".join(
        ch for ch in unicodedata.normalize("NFD", texto) if not unicodedata.combining(ch)
    )


def _tokenizar(texto: str) -> list[str]:
    # snake_case y camelCase se separan en tokens lowercase; así "compute_area"
    # y "computeArea" tokenizan igual (["compute","area"]) -> mejor solapamiento
    # léxico que un split literal. Acentos normalizados a ASCII primero (ver _sin_acentos).
    texto = _sin_acentos(texto)
    texto = texto.replace("_", " ")
    texto = re.sub(r"(?<=[a-z0-9])([A-Z])", r" \1", texto)  # camelCase -> espacio
    return _PALABRA.findall(texto.lower())


def _hash_bucket(token: str, dim: int) -> int:
    h = hashlib.sha256(token.encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big") % dim


class EmbedderTFIDF:
    """Embedder determinístico sin red: hashing-TF + L2, dimensión `dim`.

    Cada token (snake/camel spliteado, lowercase) mapea vía sha256 a un bucket;
    el vector suma la frecuencia de tokens y se normaliza L2. Gratuito,
    determinístico y testeable, pero NO semántico (ver docstring del módulo).
    """

    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim
        self.firma = f"{_ID_TFIDF}:{dim}"

    def _vector(self, texto: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in _tokenizar(texto):
            v[_hash_bucket(tok, self.dim)] += 1.0
        norma = sum(x * x for x in v) ** 0.5
        if norma == 0.0:
            return v
        return [x / norma for x in v]

    def __call__(self, textos: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in textos]


def similitud_coseno(a: list[float], b: list[float]) -> float:
    # Dims incompatibles o vector nulo -> no comparables -> 0.0 (no lanza).
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def firma_de(embedder: Any) -> str | None:
    """Identidad del embedder para invalidar el store al cambiar proveedor/dim.

    Convención "<id>:<dim>" (p.ej. "tfidf:512", "openai-3-small:1536"). None si el
    callable no declara `firma` (entonces el store lo reconstruye cada vez).
    """
    return getattr(embedder, "firma", None)
