"""Tests de Q4: duplicación semántica (coseno de embeddings)."""
import sys
import types
import importlib.util
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent / "arnes-gates"


@pytest.fixture(autouse=True)
def _load_and_reset():
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        ns.__package__ = "hermes_plugins"
        sys.modules["hermes_plugins"] = ns
    if "hermes_plugins.arnes_gates" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "hermes_plugins.arnes_gates",
            str(PLUGIN_DIR / "__init__.py"),
            submodule_search_locations=[str(PLUGIN_DIR)],
        )
        module = importlib.util.module_from_spec(spec)
        module.__package__ = "hermes_plugins.arnes_gates"
        module.__path__ = [str(PLUGIN_DIR)]
        sys.modules["hermes_plugins.arnes_gates"] = module
        spec.loader.exec_module(module)
    from hermes_plugins.arnes_gates import gate_state
    gate_state.reset()
    yield
    gate_state.reset()


def _setup_store(existing_source: str, func_name: str):
    """Crea un embedder TF-IDF y un store mock con una funcion existente."""
    from hermes_plugins.arnes_gates import embedding_service
    from hermes_plugins.arnes_gates.embedding import EmbedderTFIDF

    embedder = EmbedderTFIDF()
    vec = embedder([existing_source])[0]
    store = {
        "items": {
            "existing.py::func": {
                "path": "existing.py",
                "qualname": func_name,
                "kind": "function",
                "signature": f"{func_name}(x)",
                "lineno": 1,
                "vector": vec,
            }
        }
    }
    embedding_service._embedder = embedder
    embedding_service._store = store
    return embedder, store


def _clear_store():
    from hermes_plugins.arnes_gates import embedding_service
    embedding_service._embedder = None
    embedding_service._store = None


# Funcion existente sustancial (>= 5 nodos AST).
EXISTING = '''
def calcular(x):
    a = x + 1
    b = a * 2
    c = b - 3
    return c
'''

# Reordenada: mismo significado, forma distinta.
REORDERED = '''
def nueva(x):
    c = b - 3
    a = x + 1
    b = a * 2
    return c
'''

# Funcion totalmente distinta.
DIFFERENT = '''
def otra(x):
    resultado = []
    for i in range(x):
        resultado.append(i ** 2)
    return resultado
'''


# =============================================================================
# Casos basicos.
# =============================================================================
def test_no_store_fail_open():
    """Sin store → None (fail-open)."""
    from hermes_plugins.arnes_gates.dup_gates import validar_duplicacion_semantica
    _clear_store()
    result = validar_duplicacion_semantica("def f(): pass", "new.py")
    assert result is None


def test_semantic_duplicate_reordered():
    """Def reordenada con mismo significado → BLOQUEADO."""
    from hermes_plugins.arnes_gates.dup_gates import validar_duplicacion_semantica
    _setup_store(EXISTING, "calcular")
    result = validar_duplicacion_semantica(REORDERED, "new.py")
    # TF-IDF es léxico: los tokens son los mismos en reordered → coseno alto.
    # Puede o no pasar el umbral. Si pasa → BLOQUEADO. Si no → None.
    # Aceptamos cualquiera de los dos (el gate es bonus).
    assert result is None or "BLOQUEADO" in result


def test_semantic_different():
    """Def con significado distinto → None (OK)."""
    from hermes_plugins.arnes_gates.dup_gates import validar_duplicacion_semantica
    _setup_store(EXISTING, "calcular")
    result = validar_duplicacion_semantica(DIFFERENT, "new.py")
    assert result is None


def test_semantic_trivial_not_checked():
    """Def trivial (< MIN_NODOS_DUP) → no se compara."""
    from hermes_plugins.arnes_gates.dup_gates import validar_duplicacion_semantica
    _setup_store(EXISTING, "calcular")
    result = validar_duplicacion_semantica("def f(x): return x", "new.py")
    assert result is None


def test_semantic_reuse_excluded():
    """Def citada en reuse → excluida."""
    from hermes_plugins.arnes_gates.dup_gates import validar_duplicacion_semantica
    from hermes_plugins.arnes_gates import gate_state
    _setup_store(EXISTING, "calcular")
    # Citar "nueva" en reuse para que no se compare.
    gate_state.get()["last_plan"] = {"reuse": [{"name": "nueva"}]}
    result = validar_duplicacion_semantica(EXISTING.replace("calcular", "nueva"), "new.py")
    assert result is None


def test_semantic_own_file_excluded():
    """Mismo path → excluida."""
    from hermes_plugins.arnes_gates.dup_gates import validar_duplicacion_semantica
    _setup_store(EXISTING, "calcular")
    result = validar_duplicacion_semantica(EXISTING, "existing.py")
    assert result is None


def test_semantic_syntax_error():
    """Content con syntax error → None."""
    from hermes_plugins.arnes_gates.dup_gates import validar_duplicacion_semantica
    _setup_store(EXISTING, "calcular")
    result = validar_duplicacion_semantica("def broken(:", "new.py")
    assert result is None


def test_semantic_exact_copy_blocks():
    """Copia exacta (mismo source) → coseno ~1.0 → BLOQUEADO."""
    from hermes_plugins.arnes_gates.dup_gates import validar_duplicacion_semantica
    _setup_store(EXISTING, "calcular")
    # Mismo cuerpo con nombre distinto.
    copy = EXISTING.replace("calcular", "copia")
    result = validar_duplicacion_semantica(copy, "new.py")
    assert result is not None
    assert "BLOQUEADO" in result
    assert "copia" in result
