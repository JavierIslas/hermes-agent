"""Tests de evidence.py: gates basados en evidencia (output crudo) — Java."""
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


def _ev():
    from hermes_plugins.arnes_gates import evidence
    return evidence


# =============================================================================
# Maven surefire: "Tests run: X, Failures: Y, Errors: Z, Skipped: W"
# =============================================================================
class TestSurefire:
    def test_surefire_pass(self):
        out = (
            "[INFO] -------------------------------------------------------\n"
            "[INFO] BUILD SUCCESS\n"
            "[INFO] -------------------------------------------------------\n"
            "[INFO] Total time:  4.2 s\n"
            "[INFO] Tests run: 12, Failures: 0, Errors: 0, Skipped: 0\n"
        )
        ev = _ev().evaluate_test_evidence(out)
        assert ev.verdict == "pass"
        assert "12" in ev.reason

    def test_surefire_fail_con_failures(self):
        out = "Tests run: 12, Failures: 3, Errors: 0, Skipped: 0\n"
        ev = _ev().evaluate_test_evidence(out)
        assert ev.verdict == "fail"
        assert "3" in ev.reason

    def test_surefire_fail_con_errors(self):
        out = "Tests run: 12, Failures: 0, Errors: 2, Skipped: 0\n"
        ev = _ev().evaluate_test_evidence(out)
        assert ev.verdict == "fail"
        assert "2" in ev.reason

    def test_surefire_multi_class_agrega(self):
        # Surefire imprime una línea por clase + un resumen final. El gate debe
        # leer el TOTAL (resumen final), no solo la primera clase.
        out = (
            "[INFO] Tests run: 5, Failures: 0, Errors: 0, Skipped: 0 - in com.x.FooTest\n"
            "[INFO] Tests run: 7, Failures: 1, Errors: 0, Skipped: 0 - in com.x.BarTest\n"
            "[INFO] Results:\n"
            "[INFO] Tests run: 12, Failures: 1, Errors: 0, Skipped: 0\n"
        )
        ev = _ev().evaluate_test_evidence(out)
        assert ev.verdict == "fail"
        assert "1" in ev.reason

    def test_build_failure_sin_test_summary_es_fallo(self):
        # Falla de compilación: BUILD FAILURE sin ninguna línea "Tests run".
        out = (
            "[ERROR] Failed to execute goal ... maven-compiler-plugin ... compile\n"
            "[ERROR] COMPILATION ERROR\n"
            "[INFO] BUILD FAILURE\n"
        )
        ev = _ev().evaluate_test_evidence(out)
        assert ev.verdict == "fail"

    def test_maven_dep_no_resuelta_es_fail_open(self):
        # Fallo de environment: el runner no pudo descargar dependencias —
        # no es un test rojo. Fail-open honesto.
        out = (
            "[ERROR] Failed to execute goal on project ws: Could not resolve "
            "dependencies for project com.x:ws:jar:1.0: Could not transfer "
            "artifact javaee from central ...\n"
            "[INFO] BUILD FAILURE\n"
        )
        ev = _ev().evaluate_test_evidence(out)
        assert ev.verdict == "fail_open"

    def test_build_success_sin_tests_run_es_fail_open(self):
        # BUILD SUCCESS sin tests (ej: pom packaging sin surefire) — no mintamos
        # un verde que no vimos.
        out = "[INFO] BUILD SUCCESS\n[INFO] Total time: 2.1 s\n"
        ev = _ev().evaluate_test_evidence(out)
        assert ev.verdict == "fail_open"

    def test_build_failure_por_test_rojo_ya_cubierto_por_failures(self):
        # El caso común: surefire reporta failures y BUILD FAILURE cierra.
        out = (
            "[INFO] Results:\n"
            "[INFO] Tests run: 4, Failures: 1, Errors: 0, Skipped: 0\n"
            "[INFO] BUILD FAILURE\n"
        )
        ev = _ev().evaluate_test_evidence(out)
        assert ev.verdict == "fail"


# =============================================================================
# Gradle test runner.
# =============================================================================
class TestGradleEvidence:
    def test_gradle_pass(self):
        out = (
            "com.x.FooTest > someMethod PASSED (50)\n"
            "BUILD SUCCESSFUL in 6s\n"
            "4 actionable tasks: 4 executed\n"
        )
        ev = _ev().evaluate_test_evidence(out)
        # Gradle plain sin conteo total: BUILD SUCCESSFUL no es prueba de que
        # los tests pasaron (podría no haber corrido ninguno). Fail-open honesto.
        assert ev.verdict == "fail_open"

    def test_gradle_pass_con_conteo(self):
        out = (
            "com.x.FooTest > someMethod PASSED (50)\n"
            "3 tests completed, 0 failed\n"
            "BUILD SUCCESSFUL in 6s\n"
        )
        ev = _ev().evaluate_test_evidence(out)
        assert ev.verdict == "pass"

    def test_gradle_fail_con_failed(self):
        out = (
            "com.x.FooTest > someMethod FAILED\n"
            "    org.opentest4j.AssertionFailedError ...\n"
            "3 tests completed, 1 failed\n"
            "BUILD FAILED in 5s\n"
        )
        ev = _ev().evaluate_test_evidence(out)
        assert ev.verdict == "fail"

    def test_gradle_dep_no_resuelta_fail_open(self):
        out = (
            "* What went wrong:\n"
            "Execution failed for task ':compileJava'.\n"
            "> Could not resolve all files for configuration ':compileClasspath'.\n"
            "> Could not find com.foo:bar:1.0.\n"
            "BUILD FAILED in 3s\n"
        )
        ev = _ev().evaluate_test_evidence(out)
        assert ev.verdict == "fail_open"


# =============================================================================
# Checkstyle (run_lint).
# =============================================================================
class TestCheckstyleEvidence:
    def test_checkstyle_clean(self):
        out = (
            "[INFO] Starting audit...\n"
            "[INFO] Audit done.\n"
            "[INFO] BUILD SUCCESS\n"
        )
        ev = _ev().evaluate_lint_evidence(out)
        assert ev.verdict == "pass"

    def test_checkstyle_con_violations(self):
        out = (
            "[WARN] /src/com/x/Foo.java:12: Line has trailing spaces. [RegexpSingleline]\n"
            "[INFO] There are 2 warnings reported by checkstyle\n"
            "[INFO] BUILD FAILURE\n"
        )
        ev = _ev().evaluate_lint_evidence(out)
        assert ev.verdict == "fail"

    def test_checkstyle_error_severity(self):
        # mvn checkstyle:check con severity=error imprime [ERROR] y falla el build.
        out = (
            "[ERROR] /src/Foo.java:3: 'method def lcurly' ... [LeftCurly]\n"
            "[ERROR] There is 1 error reported by Checkstyle 9.3\n"
            "[INFO] BUILD FAILURE\n"
        )
        ev = _ev().evaluate_lint_evidence(out)
        assert ev.verdict == "fail"


# =============================================================================
# Guard de conteos en cero: strings como "0 errors" no deben parsear como
# "N errors" con N=0 y fallar el build por la vía del patrón genérico.
# =============================================================================
class TestZeroCountGuard:
    def test_zero_errors_no_es_fallo(self):
        out = "[INFO] Found 0 errors and 0 warnings.\n[INFO] BUILD SUCCESS\n"
        ev = _ev().evaluate_lint_evidence(out)
        assert ev.verdict == "pass"

    def test_output_desconocido_no_falla(self):
        out = "some custom linter output nobody parses\n"
        ev = _ev().evaluate_lint_evidence(out)
        assert ev.verdict in ("pass", "fail_open")
