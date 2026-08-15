"""Tests del language adapter registry."""
import sys
import types
import importlib.util
import tempfile
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent / "arnes-gates"


@pytest.fixture(autouse=True)
def _load():
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
    yield


def _la():
    from hermes_plugins.arnes_gates import lang_adapter
    return lang_adapter


# =============================================================================
# Registry básico.
# =============================================================================
def test_python_adapter_registered():
    """Python se auto-registra al importar."""
    la = _la()
    assert la.get_adapter("python") is not None


def test_get_adapter_for_py():
    """get_adapter_for_path devuelve Python para .py."""
    la = _la()
    adapter = la.get_adapter_for_path("foo.py")
    assert adapter is not None
    assert adapter.name == "python"


def test_get_adapter_for_unknown_ext():
    """Extensión sin adapter → None."""
    la = _la()
    assert la.get_adapter_for_path("foo.xyz") is None


def test_all_extensions_has_py():
    """all_extensions incluye .py."""
    la = _la()
    assert ".py" in la.all_extensions()


def test_all_extensions_excludes_unknown():
    """No hay extensiones raras registradas."""
    la = _la()
    assert ".xyz" not in la.all_extensions()
    assert ".js" not in la.all_extensions()  # no adapter yet


# =============================================================================
# Registro de adapter custom (para testear extensibilidad).
# =============================================================================
class FakeJSAdapter:
    @property
    def name(self):
        return "javascript"

    @property
    def extensions(self):
        return frozenset({".js", ".jsx"})

    def parse_source(self, source, rel):
        return None

    def check_syntax(self, source, path):
        return None

    def extract_usages(self, content):
        return set()


def test_register_custom_adapter():
    """Se puede registrar un adapter custom."""
    la = _la()
    original_js = la.get_adapter("javascript")
    adapter = FakeJSAdapter()
    la.register(adapter)
    try:
        assert la.get_adapter("javascript") is adapter
        assert ".js" in la.all_extensions()
        assert ".jsx" in la.all_extensions()
        assert la.get_adapter_for_path("app.js") is adapter
    finally:
        # Cleanup: remover el adapter del registry.
        la._registry.pop("javascript", None)
        for ext in [".js", ".jsx"]:
            la._ext_map.pop(ext, None)
    assert la.get_adapter("javascript") is None or original_js is None


# =============================================================================
# Auto-detección de stack.
# =============================================================================
def test_detect_stack_python(tmp_path):
    """Proyecto con pyproject.toml → python."""
    la = _la()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
    assert "python" in la.detect_stack(tmp_path)


def test_detect_stack_javascript(tmp_path):
    """Proyecto con package.json → javascript."""
    la = _la()
    (tmp_path / "package.json").write_text('{"name":"test"}')
    assert "javascript" in la.detect_stack(tmp_path)


def test_detect_stack_multiple(tmp_path):
    """Proyecto con pyproject.toml + package.json → python + javascript."""
    la = _la()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
    (tmp_path / "package.json").write_text('{"name":"test"}')
    stacks = la.detect_stack(tmp_path)
    assert "python" in stacks
    assert "javascript" in stacks


def test_detect_stack_go(tmp_path):
    """Proyecto con go.mod → go."""
    la = _la()
    (tmp_path / "go.mod").write_text("module test\n")
    assert "go" in la.detect_stack(tmp_path)


def test_detect_stack_none(tmp_path):
    """Proyecto sin marcadores → []."""
    la = _la()
    assert la.detect_stack(tmp_path) == []


def test_detect_stack_python_by_py_files(tmp_path):
    """Proyecto sin marcadores pero con .py → python por defecto."""
    la = _la()
    (tmp_path / "main.py").write_text("print('hello')\n")
    assert "python" in la.detect_stack(tmp_path)


def test_adapters_for_root_python(tmp_path):
    """adapters_for_root devuelve [PythonAdapter] para proyecto Python."""
    la = _la()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
    adapters = la.adapters_for_root(tmp_path)
    assert len(adapters) == 1
    assert adapters[0].name == "python"


def test_adapters_for_root_unknown_falls_back_python(tmp_path):
    """Proyecto sin stack conocido → fallback a Python."""
    la = _la()
    adapters = la.adapters_for_root(tmp_path)
    assert len(adapters) == 1
    assert adapters[0].name == "python"


# =============================================================================
# PythonAdapter.
# =============================================================================
def test_python_adapter_parse_source():
    """PythonAdapter.parse_source parsea código Python."""
    la = _la()
    adapter = la.get_adapter("python")
    result = adapter.parse_source("def foo(x): return x + 1\n", "mod.py")
    assert result is not None
    assert "symbols" in result


def test_python_adapter_check_syntax_ok():
    la = _la()
    adapter = la.get_adapter("python")
    assert adapter.check_syntax("x = 1\n", "test.py") is None


def test_python_adapter_check_syntax_error():
    la = _la()
    adapter = la.get_adapter("python")
    result = adapter.check_syntax("def broken(:\n", "test.py")
    assert result is not None
    assert "sintaxis" in result.lower() or "syntax" in result.lower()


def test_python_adapter_extract_usages():
    la = _la()
    adapter = la.get_adapter("python")
    usages = adapter.extract_usages("result = foo(bar)")
    assert "foo" in usages
    assert "bar" in usages
