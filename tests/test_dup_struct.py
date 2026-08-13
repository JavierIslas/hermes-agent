"""Tests de Q3: duplicación estructural (struct_hash)."""
import sys
import types
import importlib.util
import ast
import hashlib
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


def _compute_struct_hash(source: str, func_name: str) -> str | None:
    """Computa el struct_hash de una funcion en source (igual que index.py)."""
    from hermes_plugins.arnes_gates.index import _normalizar_def, MIN_NODOS_DUP
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            canonical, ncount = _normalizar_def(node)
            if ncount < MIN_NODOS_DUP:
                return None
            return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return None


def _set_index_with_func(source: str, func_name: str, rel_path: str = "existing.py"):
    """Crea un indice mock con una funcion existente y su struct_hash."""
    from hermes_plugins.arnes_gates import index_service
    sh = _compute_struct_hash(source, func_name)
    index = {
        "version": 4,
        "root": "/fake",
        "files": {
            rel_path: {
                "path": rel_path,
                "symbols": [{
                    "name": func_name,
                    "qualname": func_name,
                    "kind": "function",
                    "lineno": 1,
                    "end_lineno": 10,
                    "signature": f"{func_name}(x)",
                    "params": [{"name": "x", "type": None}],
                    "returns": None,
                    "struct_hash": sh,
                }],
            }
        },
    }
    index_service._index = index
    index_service._project_root = Path("/fake")
    return index


def _clear_index():
    from hermes_plugins.arnes_gates import index_service
    index_service._index = None
    index_service._project_root = None


# Funcion existente sustancial (>= 5 nodos AST).
EXISTING_FUNC = '''
def calcular(x):
    a = x + 1
    b = a * 2
    c = b - 3
    return c
'''


# =============================================================================
# Casos basicos.
# =============================================================================
def test_no_index_fail_open():
    """Sin indice → None (fail-open)."""
    from hermes_plugins.arnes_gates.dup_gates import validar_duplicacion
    _clear_index()
    result = validar_duplicacion("def foo(): pass", "new.py")
    assert result is None


def test_duplicate_detected():
    """Def nueva con mismo cuerpo que existente → BLOQUEADO."""
    from hermes_plugins.arnes_gates.dup_gates import validar_duplicacion
    _set_index_with_func(EXISTING_FUNC, "calcular")
    # Mismo cuerpo, distinto nombre.
    new_code = EXISTING_FUNC.replace("calcular", "nueva_func")
    result = validar_duplicacion(new_code, "new.py")
    assert result is not None
    assert "BLOQUEADO" in result
    assert "nueva_func" in result
    assert "calcular" in result


def test_no_duplicate_different_body():
    """Def nueva con cuerpo distinto → None (OK)."""
    from hermes_plugins.arnes_gates.dup_gates import validar_duplicacion
    _set_index_with_func(EXISTING_FUNC, "calcular")
    new_code = '''
def otra_func(x):
    return x ** 2 + x * 3 - 1
'''
    result = validar_duplicacion(new_code, "new.py")
    assert result is None


def test_trivial_function_not_checked():
    """Def trivial (< MIN_NODOS_DUP) → no se compara."""
    from hermes_plugins.arnes_gates.dup_gates import validar_duplicacion
    _set_index_with_func(EXISTING_FUNC, "calcular")
    # Funcion trivial de 1 linea.
    result = validar_duplicacion("def f(x): return x", "new.py")
    assert result is None


def test_reuse_excluded():
    """Def citada en reuse → no se reporta como duplicada."""
    from hermes_plugins.arnes_gates.dup_gates import validar_duplicacion
    from hermes_plugins.arnes_gates import gate_state
    _set_index_with_func(EXISTING_FUNC, "calcular")
    gate_state.get()["last_plan"] = {"reuse": [{"name": "nueva_func"}]}
    new_code = EXISTING_FUNC.replace("calcular", "nueva_func")
    result = validar_duplicacion(new_code, "new.py")
    assert result is None


def test_own_file_excluded():
    """Reescribir tu propia def en el mismo archivo → no es dup."""
    from hermes_plugins.arnes_gates.dup_gates import validar_duplicacion
    _set_index_with_func(EXISTING_FUNC, "calcular", rel_path="existing.py")
    # Mismo path + mismo nombre → no es dup.
    result = validar_duplicacion(EXISTING_FUNC, "existing.py")
    assert result is None


def test_syntax_error_content():
    """Content con syntax error → _buscar_duplicados devuelve []."""
    from hermes_plugins.arnes_gates.dup_gates import validar_duplicacion
    _set_index_with_func(EXISTING_FUNC, "calcular")
    result = validar_duplicacion("def broken(:", "new.py")
    assert result is None
