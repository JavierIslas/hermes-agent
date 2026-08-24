"""Tests de detección de runners Java: maven, gradle, wrappers, checkstyle."""
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
# Maven: pom.xml → mvnw (si existe) o mvn.
# =============================================================================
class TestMaven:
    def test_pom_xml_detecta_maven(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project></project>", encoding="utf-8")
        command, cwd = _verif()._detect_tests_setup(tmp_path)
        assert command is not None
        assert "maven" in " ".join(command) or "mvn" in " ".join(command)
        assert cwd == tmp_path

    def test_mvnw_tiene_prioridad(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project></project>", encoding="utf-8")
        (tmp_path / "mvnw").write_text("#!/bin/sh\n", encoding="utf-8")
        command, _ = _verif()._detect_tests_setup(tmp_path)
        assert command[0] == "./mvnw"

    def test_mvn_sin_wrapper(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project></project>", encoding="utf-8")
        command, _ = _verif()._detect_tests_setup(tmp_path)
        assert command[0] == "mvn"


# =============================================================================
# Gradle: build.gradle / build.gradle.kts → gradlew o gradle.
# =============================================================================
class TestGradle:
    def test_build_gradle_detecta_gradle(self, tmp_path):
        (tmp_path / "build.gradle").write_text("plugins {}\n", encoding="utf-8")
        command, _ = _verif()._detect_tests_setup(tmp_path)
        assert command is not None
        assert "gradle" in " ".join(command)

    def test_build_gradle_kts_detecta_gradle(self, tmp_path):
        (tmp_path / "build.gradle.kts").write_text("plugins {}\n", encoding="utf-8")
        command, _ = _verif()._detect_tests_setup(tmp_path)
        assert command is not None
        assert "gradle" in " ".join(command)

    def test_gradlew_tiene_prioridad(self, tmp_path):
        (tmp_path / "build.gradle").write_text("plugins {}\n", encoding="utf-8")
        (tmp_path / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
        command, _ = _verif()._detect_tests_setup(tmp_path)
        assert command[0] == "./gradlew"

    def test_sin_manifest_java_no_detecta(self, tmp_path):
        command, _ = _verif()._detect_tests_setup(tmp_path)
        assert command is None


# =============================================================================
# Lint Java: checkstyle (config explícita o plugin en pom).
# =============================================================================
class TestLintJava:
    def test_checkstyle_xml_detecta_checkstyle(self, tmp_path):
        (tmp_path / "checkstyle.xml").write_text(
            '<module name="Checker"></module>', encoding="utf-8"
        )
        command, _ = _verif()._detect_lint_setup(tmp_path)
        assert command is not None
        assert "checkstyle" in " ".join(command)

    def test_pom_con_plugin_checkstyle_detecta(self, tmp_path):
        (tmp_path / "pom.xml").write_text(
            "<project><artifactId>checkstyle-maven-plugin</artifactId></project>",
            encoding="utf-8",
        )
        command, _ = _verif()._detect_lint_setup(tmp_path)
        assert command is not None
        assert "checkstyle" in " ".join(command)

    def test_pom_sin_checkstyle_sin_eslint_sin_linter(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project></project>", encoding="utf-8")
        command, _ = _verif()._detect_lint_setup(tmp_path)
        assert command is None

    def test_gradle_con_plugin_checkstyle(self, tmp_path):
        (tmp_path / "build.gradle").write_text(
            "apply plugin: 'checkstyle'\n", encoding="utf-8"
        )
        command, _ = _verif()._detect_lint_setup(tmp_path)
        assert command is not None
        assert "checkstyle" in " ".join(command)


# =============================================================================
# Prioridad del stack: un repo Python+Java no debe pisarse entre runners.
# =============================================================================
class TestPrioridad:
    def test_pytest_gana_sobre_maven(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\n", encoding="utf-8"
        )
        (tmp_path / "pom.xml").write_text("<project></project>", encoding="utf-8")
        command, _ = _verif()._detect_tests_setup(tmp_path)
        assert "pytest" in " ".join(command)

    def test_java_gana_sobre_make(self, tmp_path):
        (tmp_path / "Makefile").write_text("test:\n\techo hi\n", encoding="utf-8")
        (tmp_path / "pom.xml").write_text("<project></project>", encoding="utf-8")
        command, _ = _verif()._detect_tests_setup(tmp_path)
        assert "mvn" in " ".join(command) or "maven" in " ".join(command)
