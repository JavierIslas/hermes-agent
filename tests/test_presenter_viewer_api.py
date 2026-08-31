"""Tests del API del viewer del par worker/presenter (dashboard plugin).

F5.9: /api/plugins/arnes-viewer/pairs y /stats leen
HERMES_HOME/presenter_pairs.jsonl. Capa fina de LECTURA: sin JSONL el
endpoint responde lista vacía (no trona), línea corrupta se salta.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


@pytest.fixture()
def viewer_api(tmp_path, monkeypatch):
    """Carga plugin_api.py con HERMES_HOME=tmp_path (mismo patrón conftest)."""
    import importlib.util
    import sys

    from pathlib import Path
    api_file = Path(__file__).resolve().parent.parent / "plugins" / "arnes-gates" / "dashboard" / "plugin_api.py"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    # plugin_api importa hermes_constants.get_hermes_home — en el entorno de
    # test existe (venv del repo). Stub mínimo si no:
    try:
        import hermes_constants  # noqa: F401
    except ImportError:
        hc = SimpleNamespace(
            get_hermes_home=lambda: tmp_path,
        )
        sys.modules["hermes_constants"] = hc

    spec = importlib.util.spec_from_file_location("arnes_viewer_api", str(api_file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestPairs:
    def test_sin_jsonl_responde_lista_vacia(self, viewer_api):
        entries = viewer_api._load_entries()
        assert entries == []

    def test_lee_entradas_mas_reciente_primero(self, viewer_api, tmp_path):
        lines = [
            {"ts": "2026-08-31T14:42:00+00:00", "outcome": "dressed",
             "raw": "a", "dressed": "b", "user_message": "m1", "tool_calls": 4},
            {"ts": "2026-08-31T15:00:00+00:00", "outcome": "failopen:llm",
             "raw": "c", "dressed": None, "user_message": "m2", "tool_calls": 0},
        ]
        (tmp_path / "presenter_pairs.jsonl").write_text(
            "\n".join(json.dumps(l) for l in lines), encoding="utf-8")
        entries = viewer_api._load_entries()
        assert len(entries) == 2
        assert entries[0]["ts"].startswith("2026-08-31T15:00")  # reciente 1º
        assert entries[1]["outcome"] == "dressed"

    def test_linea_corrupta_se_salta(self, viewer_api, tmp_path):
        ok = json.dumps({"ts": "t1", "outcome": "dressed", "raw": "r",
                         "dressed": "d", "user_message": "u", "tool_calls": 1})
        (tmp_path / "presenter_pairs.jsonl").write_text(
            ok + "\n{corrupta...\n", encoding="utf-8")
        entries = viewer_api._load_entries()
        assert len(entries) == 1

    def test_tope_de_200_entradas(self, viewer_api, tmp_path):
        lines = [json.dumps({"ts": f"t{i:04d}", "outcome": "dressed",
                             "raw": "r", "dressed": "d",
                             "user_message": "u", "tool_calls": 0})
                 for i in range(250)]
        (tmp_path / "presenter_pairs.jsonl").write_text(
            "\n".join(lines), encoding="utf-8")
        entries = viewer_api._load_entries()
        assert len(entries) == 200
        assert entries[0]["ts"] == "t0249"  # las últimas 200, reciente 1º


class TestStats:
    def test_cuenta_outcomes(self, viewer_api, tmp_path):
        lines = [
            {"ts": "t1", "outcome": "dressed"},
            {"ts": "t2", "outcome": "dressed"},
            {"ts": "t3", "outcome": "failopen:integrity"},
        ]
        (tmp_path / "presenter_pairs.jsonl").write_text(
            "\n".join(json.dumps(l) for l in lines), encoding="utf-8")

        import asyncio
        stats = asyncio.run(viewer_api.get_stats())
        assert stats["total"] == 3
        assert stats["outcomes"]["dressed"] == 2
        assert stats["outcomes"]["failopen:integrity"] == 1
