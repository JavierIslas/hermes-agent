"""Tests de _on_post_tool_call: indice incremental + cd-tracking + writes.

Regresion del bug D9 (2026-08-17): el hook usaba ``Path`` sin importarlo a
nivel de modulo (el unico import vivia local en ``_exists_unread``), asi que
todo ``cd`` en el terminal levantaba ``NameError: name 'Path' is not
defined``. El core hace fail-open del hook, asi que el bug mato en silencio
el cd-tracking Y el registro de written_paths desde el 13/08 (D2).

Los servicios de indice se monkeypatchean: estos tests son unitarios al
hook, no integran index_service.build_for (que indexa el arbol entero).
"""
import sys
import types
import importlib.util
from pathlib import Path

import pytest

# CANONICA del plugin dentro del repo (no la copia huérfana de afuera).
PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "arnes-gates"


@pytest.fixture(scope="module", autouse=True)
def _plugin():
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
    return sys.modules["hermes_plugins.arnes_gates"]


@pytest.fixture(autouse=True)
def _reset():
    yield
    sys.modules["hermes_plugins.arnes_gates"].state.reset()


@pytest.fixture
def builds(monkeypatch, _plugin):
    """Espia build_for de ambos servicios (sin indexar nada real).

    El fake de index_service setea _project_root como lo hace el build_for
    real, para que la deduplicacion del hook (get_root != new_cwd) funcione.
    """
    calls = {"index": [], "embedding": []}

    def _fake_index_build(root):
        _plugin.index_service._project_root = Path(root)
        calls["index"].append(str(root))

    # Estado limpio por test: sin esto, _project_root sobrevive de un test
    # al siguiente (global de modulo) y la deduplicacion salta la primera
    # construccion (falso negativo por polucion entre tests).
    _plugin.index_service._project_root = None
    monkeypatch.setattr(_plugin.index_service, "build_for", _fake_index_build)
    monkeypatch.setattr(
        _plugin.embedding_service,
        "build_for",
        lambda root: calls["embedding"].append(str(root)),
    )
    return calls


class TestPostToolCallCdTracking:
    def test_cd_no_explota_el_hook(self, _plugin, builds):
        """Un cd simple no debe levantar NameError (regresion D9)."""
        plugin = _plugin
        plugin.state.reset()
        # RED: con el bug, esto levanta NameError: name 'Path' is not defined
        plugin._on_post_tool_call(
            tool_name="terminal",
            args={"command": "cd /tmp"},
            result={"output": ""},
        )
        assert plugin.state.get()["terminal_cwd"] == "/tmp"
        # Lazy-build: el indice del nuevo proyecto se construye una sola vez.
        assert builds["index"] == ["/tmp"]
        assert builds["embedding"] == ["/tmp"]

    def test_cd_segundo_cwd_no_reconstruye(self, _plugin, builds):
        """cd al mismo proyecto otra vez: el indice NO se reconstruye."""
        plugin = _plugin
        plugin.state.reset()
        for _ in range(2):
            plugin._on_post_tool_call(
                tool_name="terminal",
                args={"command": "cd /tmp && ls"},
                result={"output": ""},
            )
        assert builds["index"] == ["/tmp"]  # una sola vez

    def test_cd_y_write_registran_ambas_cosas(self, _plugin, builds):
        plugin = _plugin
        plugin.state.reset()
        plugin._on_post_tool_call(
            tool_name="terminal",
            args={"command": "cd /workspace && cp a.txt b.txt"},
            result={"output": ""},
        )
        gate = plugin.state.get()
        assert gate["terminal_cwd"] == "/workspace"
        assert "b.txt" in gate["written_paths"]

    def test_write_sin_cd_registra_target(self, _plugin, builds):
        plugin = _plugin
        plugin.state.reset()
        plugin._on_post_tool_call(
            tool_name="terminal",
            args={"command": "cp origen.txt /tmp/destino.txt"},
            result={"output": ""},
        )
        assert "/tmp/destino.txt" in plugin.state.get()["written_paths"]

    def test_cd_subdir_relativo_resuelve(self, _plugin, builds):
        """cd relativo (typico tras un cd absoluto previo)."""
        plugin = _plugin
        plugin.state.reset()
        plugin._on_post_tool_call(
            tool_name="terminal",
            args={"command": "cd /workspace"},
            result={"output": ""},
        )
        # patchear get_root para simular indice ya construido
        orig_root = plugin.index_service.get_root
        plugin.index_service.get_root = lambda: Path("/workspace")
        try:
            plugin._on_post_tool_call(
                tool_name="terminal",
                args={"command": "cd subdir && make"},
                result={"output": ""},
            )
        finally:
            plugin.index_service.get_root = orig_root
        assert plugin.state.get()["terminal_cwd"] == "/workspace/subdir"


class TestPostToolCallWriteFile:
    def test_write_file_actualiza_indice(self, _plugin, monkeypatch):
        """write_file con path: update_file de ambos servicios, sin excepcion."""
        plugin = _plugin
        plugin.state.reset()
        seen = []
        monkeypatch.setattr(
            plugin.index_service, "update_file", lambda p: seen.append(("index", p))
        )
        monkeypatch.setattr(
            plugin.embedding_service, "update_file", lambda p: seen.append(("emb", p))
        )
        plugin._on_post_tool_call(
            tool_name="write_file",
            args={"path": "/tmp/algo.py"},
            result={"bytes_written": 1},
        )
        assert ("index", "/tmp/algo.py") in seen
        assert ("emb", "/tmp/algo.py") in seen

    def test_write_file_sin_path_no_hace_nada(self, _plugin, monkeypatch):
        plugin = _plugin
        plugin.state.reset()
        called = []
        monkeypatch.setattr(
            plugin.index_service, "update_file", lambda p: called.append(p)
        )
        plugin._on_post_tool_call(
            tool_name="write_file", args={}, result={"bytes_written": 1}
        )
        assert called == []
        assert plugin.state.get()["written_paths"] == set()
