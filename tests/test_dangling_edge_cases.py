"""Tests de find_dangling: falsos positivos que mordieron en producción (2026-08-17).

El commit gate (Q5) bloqueó un commit legítimo con 64 "imports colgantes":
  1. `.hermes-runtime/` — runtime de Python que uv descarga DENTRO del repo —
     indexado como si fuera código del proyecto (pip/setuptools vendored).
  2. Símbolos listados en `__all__` pero no re-conocidos como vinculados.
Estos tests fijan ambos comportamientos (RED antes del fix).
"""
from __future__ import annotations

from pathlib import Path

# arnes_plugin: fixture module-scoped definida en tests/conftest.py (fuente
# única tras el reclamo de duplicación Q3/Q4) — pytest la descubre sola.


def _build_index_for(plugin, tmp_path: Path) -> dict:
    """Índice del proyecto sintético vía el builder del plugin."""
    index_service = plugin.index_service
    saved = getattr(index_service, "_project_root", None)
    index_service._project_root = tmp_path
    try:
        from hermes_plugins.arnes_gates.index import build_index

        return build_index(tmp_path)
    finally:
        index_service._project_root = saved


class TestDanglingExcludes:
    def test_hermes_runtime_no_se_indexa(self, arnes_plugin, tmp_path):
        """.hermes-runtime (runtime de uv) no debe entrar al índice."""
        (tmp_path / "app.py").write_text("X = 1\n", encoding="utf-8")
        runtime = tmp_path / ".hermes-runtime" / "python" / "cpython-3.11"
        runtime.mkdir(parents=True)
        (runtime / "mod.py").write_text(
            "from app import INEXISTENTE  # import colgante DENTRO del runtime\n",
            encoding="utf-8",
        )
        idx = _build_index_for(arnes_plugin, tmp_path)
        files = set(idx["files"].keys())
        assert "app.py" in files
        assert not any(".hermes-runtime" in f for f in files), files

    def test_bk_archive_no_se_indexa(self, arnes_plugin, tmp_path):
        """bk/ (archivo del clon anterior con su venv) no debe entrar al índice.

        Dogfood 2026-08-19: la mudanza dejó /opt/data/hermes-agent/bk/ con el
        clon upstream+homelab VIEJO (HEAD ca6b1b35a) incluyendo su venv con
        site-packages completo — el índice lo trató como código del proyecto
        y el commit gate bloqueó con 168 falsos dangling (PIL/aiohttp/...).
        Mismo shape que D10 (.hermes-runtime): directorio de archivo no es
        codebase.
        """
        (tmp_path / "app.py").write_text("X = 1\n", encoding="utf-8")
        venv = tmp_path / "bk" / "venv" / "lib" / "python3.11" / "site-packages"
        venv.mkdir(parents=True)
        (venv / "pil_like.py").write_text(
            "from app import INEXISTENTE  # import colgante DENTRO del archivo\n",
            encoding="utf-8",
        )
        idx = _build_index_for(arnes_plugin, tmp_path)
        files = set(idx["files"].keys())
        assert "app.py" in files
        assert not any(f.startswith("bk/") for f in files), files

    def test_all_reexport_no_es_dangling(self, arnes_plugin, tmp_path):
        """`from app import X` donde app define X y lo lista en __all__:
        ningún dangling (el __all__ es una vinculación más)."""
        (tmp_path / "main.py").write_text(
            "from app import helper_fn\nfrom app import OTHER\n",
            encoding="utf-8",
        )
        (tmp_path / "app.py").write_text(
            "def helper_fn():\n    pass\n\nOTHER = 2\n__all__ = ['helper_fn', 'OTHER']\n",
            encoding="utf-8",
        )
        from hermes_plugins.arnes_gates.index import find_dangling

        idx = _build_index_for(arnes_plugin, tmp_path)
        assert find_dangling(idx, tmp_path) == []

    def test_dangling_real_sigue_detectado(self, arnes_plugin, tmp_path):
        """Control: un dangling REAL no debe dejar de detectarse tras el fix."""
        (tmp_path / "main.py").write_text(
            "from app import no_existe\n", encoding="utf-8"
        )
        (tmp_path / "app.py").write_text("X = 1\n", encoding="utf-8")
        from hermes_plugins.arnes_gates.index import find_dangling

        idx = _build_index_for(arnes_plugin, tmp_path)
        dangling = find_dangling(idx, tmp_path)
        assert len(dangling) == 1
        assert dangling[0]["tipo"] == "simbolo"
        assert dangling[0]["faltantes"] == ["no_existe"]


class TestAllDunderInConsumer:
    def test_pep562_getattr_resuelve_todo(self, arnes_plugin, tmp_path):
        """PEP 562: un módulo con __getattr__ a nivel módulo resuelve nombres
        dinámicamente (skills_hub expone SKILLS_DIR/HUB_DIR así). Un from-import
        de esos nombres NO es dangling — es imposible resolverlo estáticamente
        y el gate debe hacer fail-open (falso positivo de producción, 17/08)."""
        (tmp_path / "dyn.py").write_text(
            "_DYNAMIC = {'SKILLS_DIR': lambda: '/x'}\n\n\ndef __getattr__(name):\n    return _DYNAMIC[name]()\n",
            encoding="utf-8",
        )
        (tmp_path / "consumer.py").write_text(
            "from dyn import SKILLS_DIR\n", encoding="utf-8"
        )
        from hermes_plugins.arnes_gates.index import find_dangling

        idx = _build_index_for(arnes_plugin, tmp_path)
        assert find_dangling(idx, tmp_path) == []
