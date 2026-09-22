"""Tests de detección de runners Flutter/Dart: flutter test/analyze, dart test."""
import sys
import types
import importlib.util
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "arnes-gates"


@pytest.fixture(scope="module", autouse=True)
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


def _verif():
    from hermes_plugins.arnes_gates import verification
    return verification


# =============================================================================
# pubspec.yaml → flutter (dep sdk) o dart (puro).
# =============================================================================
class TestDeteccionFlutter:
    def test_pubspec_con_flutter_sdk_detecta_flutter_test(self, tmp_path):
        (tmp_path / "pubspec.yaml").write_text(
            "name: app\ndependencies:\n  flutter:\n    sdk: flutter\n",
            encoding="utf-8",
        )
        command, cwd = _verif()._detect_tests_setup(tmp_path)
        assert command is not None
        assert command[0].endswith("/flutter") or command[0] == "flutter"
        assert command[1:] == ["test"]
        assert cwd == tmp_path

    def test_pubspec_con_flutter_test_dev_dep_detecta_flutter_test(self, tmp_path):
        # flutter_test en dev_dependencies también implica runner flutter.
        (tmp_path / "pubspec.yaml").write_text(
            "name: app\ndev_dependencies:\n  flutter_test:\n    sdk: flutter\n",
            encoding="utf-8",
        )
        command, _ = _verif()._detect_tests_setup(tmp_path)
        assert command is not None
        assert "flutter" in Path(command[0]).name

    def test_pubspec_dart_puro_detecta_dart_test(self, tmp_path):
        (tmp_path / "pubspec.yaml").write_text(
            "name: cli\nenvironment:\n  sdk: ^3.5.0\n",
            encoding="utf-8",
        )
        command, _ = _verif()._detect_tests_setup(tmp_path)
        assert command is not None
        assert Path(command[0]).name == "dart"
        assert command[1:] == ["test"]

    def test_flutter_gana_sobre_make(self, tmp_path):
        # Un Makefile en un repo Flutter es atajo, no el runner canónico
        # (misma política que Java desde 2026-08-21).
        (tmp_path / "pubspec.yaml").write_text(
            "dependencies:\n  flutter:\n    sdk: flutter\n", encoding="utf-8"
        )
        (tmp_path / "Makefile").write_text("test:\n\tflutter test\n", encoding="utf-8")
        command, _ = _verif()._detect_tests_setup(tmp_path)
        assert "flutter" in Path(command[0]).name

    def test_pubspec_yaml_malformado_no_explota(self, tmp_path):
        (tmp_path / "pubspec.yaml").write_text(":\n::no yaml::\n", encoding="utf-8")
        # No debe levantar; cualquier veredicto honesto sirve.
        _verif()._detect_tests_setup(tmp_path)


# =============================================================================
# Resolución del binario flutter: PATH → /opt/flutter (imagen) →
# /opt/data/flutter-sdk (volumen).
# =============================================================================
class TestResolucionBinario:
    def test_path_gana(self, monkeypatch):
        v = _verif()
        monkeypatch.setattr(v.shutil, "which", lambda name: "/usr/bin/flutter")
        monkeypatch.setattr(v, "_FLUTTER_CANDIDATOS", ("/no/existe/flutter",))
        assert v._flutter_for() == "/usr/bin/flutter"

    def test_fallback_directorios(self, monkeypatch, tmp_path):
        v = _verif()
        bin_dir = tmp_path / "flutter-sdk" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "flutter").write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(v.shutil, "which", lambda name: None)
        monkeypatch.setattr(v, "_FLUTTER_CANDIDATOS", (str(bin_dir / "flutter"),))
        assert v._flutter_for() == str(bin_dir / "flutter")

    def test_no_hay_flutter_none(self, monkeypatch):
        v = _verif()
        monkeypatch.setattr(v.shutil, "which", lambda name: None)
        monkeypatch.setattr(v, "_FLUTTER_CANDIDATOS", ("/no/existe/flutter",))
        assert v._flutter_for() is None

    def test_dart_prefiere_el_del_sdk_flutter(self, monkeypatch, tmp_path):
        # dart alineado con el SDK de flutter (mismo root), no un dart suelto.
        v = _verif()
        sdk = tmp_path / "flutter-sdk"
        (sdk / "bin").mkdir(parents=True)
        (sdk / "bin" / "flutter").write_text("", encoding="utf-8")
        (sdk / "bin" / "dart").write_text("", encoding="utf-8")
        monkeypatch.setattr(v.shutil, "which", lambda name: None)
        monkeypatch.setattr(v, "_FLUTTER_CANDIDATOS", (str(sdk / "bin" / "flutter"),))
        assert v._dart_for() == str(sdk / "bin" / "dart")


# =============================================================================
# Lint: flutter analyze / dart analyze.
# =============================================================================
class TestLintFlutter:
    def test_pubspec_flutter_detecta_flutter_analyze(self, tmp_path):
        (tmp_path / "pubspec.yaml").write_text(
            "dependencies:\n  flutter:\n    sdk: flutter\n", encoding="utf-8"
        )
        command, cwd = _verif()._detect_lint_setup(tmp_path)
        assert command is not None
        assert "analyze" in command
        assert "flutter" in Path(command[0]).name
        assert "--no-pub" not in command
        assert cwd == tmp_path

    def test_pubspec_dart_puro_detecta_dart_analyze(self, tmp_path):
        (tmp_path / "pubspec.yaml").write_text(
            "name: cli\nenvironment:\n  sdk: ^3.5.0\n", encoding="utf-8"
        )
        command, _ = _verif()._detect_lint_setup(tmp_path)
        assert command is not None
        assert "analyze" in command
        assert Path(command[0]).name == "dart"

    def test_sin_pubspec_no_detecta_lint(self, tmp_path):
        command, _ = _verif()._detect_lint_setup(tmp_path)
        assert command is None


# =============================================================================
# Timeout extendido: el compile frío de flutter test supera los 120s default.
# =============================================================================
class TestTimeout:
    def test_flutter_test_recibe_timeout_extendido(self):
        v = _verif()
        assert v._timeout_for(["/opt/flutter/bin/flutter", "test"]) == v._TIMEOUT_FLUTTER
        assert v._timeout_for(["dart", "test"]) == v._TIMEOUT_FLUTTER

    def test_pytest_mantiene_timeout_default(self):
        v = _verif()
        assert v._timeout_for(["python", "-m", "pytest"]) == v._TIMEOUT
        assert v._timeout_for(["mvn", "test"]) == v._TIMEOUT


# =============================================================================
# _detect_project_root sin git: el proyecto Flutter del usuario
# (ProyectoMagicTCG) NO es repo git — la resolución por manifiesto es
# lo que hace visible el runner. En repos git, comportamiento intacto.
# =============================================================================
class TestRootSinGit:
    def test_terminal_cwd_en_dir_con_pubspec_resuelve_ese_dir(self, tmp_path, monkeypatch):
        from hermes_plugins.arnes_gates import state as gate_state
        mobile = tmp_path / "mobile"
        mobile.mkdir()
        (mobile / "pubspec.yaml").write_text(
            "dependencies:\n  flutter:\n    sdk: flutter\n", encoding="utf-8"
        )
        monkeypatch.setattr(gate_state, "_state", {"terminal_cwd": str(mobile)})
        root = _verif()._detect_project_root()
        assert root == mobile

    def test_terminal_cwd_en_subdir_resuelve_el_dir_del_manifiesto(self, tmp_path, monkeypatch):
        from hermes_plugins.arnes_gates import state as gate_state
        mobile = tmp_path / "mobile"
        sub = mobile / "lib" / "src"
        sub.mkdir(parents=True)
        (mobile / "pubspec.yaml").write_text("name: app\n", encoding="utf-8")
        monkeypatch.setattr(gate_state, "_state", {"terminal_cwd": str(sub)})
        root = _verif()._detect_project_root()
        assert root == mobile

    def test_repo_git_mantiene_git_root_aunque_haya_pubspec_en_subdir(
        self, tmp_path, monkeypatch
    ):
        import subprocess
        from hermes_plugins.arnes_gates import state as gate_state
        repo = tmp_path / "repo"
        (repo / "mobile").mkdir(parents=True)
        (repo / "mobile" / "pubspec.yaml").write_text("name: app\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        monkeypatch.setattr(
            gate_state, "_state", {"terminal_cwd": str(repo / "mobile")}
        )
        root = _verif()._detect_project_root()
        assert root == repo
