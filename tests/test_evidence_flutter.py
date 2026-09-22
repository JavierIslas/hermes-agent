"""Tests de evidence.py: gates basados en evidencia (output crudo) - Flutter/Dart."""
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
# flutter test: "00:03 +8: All tests passed!" / "Some tests failed."
# =============================================================================
class TestFlutterTests:
    def test_pass(self):
        out = "00:03 +8: All tests passed!\n"
        ev = _ev().evaluate_test_evidence(out)
        assert ev.verdict == "pass"
        assert "8" in ev.reason

    def test_pass_con_carga_previa(self):
        out = (
            "00:01 +0: loading test/foo_test.dart\n"
            "00:04 +3: test/foo_test.dart\n"
            "00:06 +8: All tests passed!\n"
        )
        ev = _ev().evaluate_test_evidence(out)
        assert ev.verdict == "pass"
        assert "8" in ev.reason

    def test_fail(self):
        out = "00:05 +6 -2: Some tests failed.\n"
        ev = _ev().evaluate_test_evidence(out)
        assert ev.verdict == "fail"
        assert "2" in ev.reason

    def test_solo_failed(self):
        out = "00:05 -3: Some tests failed.\n"
        ev = _ev().evaluate_test_evidence(out)
        assert ev.verdict == "fail"
        assert "3" in ev.reason

    def test_cero_tests_no_es_verde(self):
        out = "00:02 +0: All tests passed!\n"
        ev = _ev().evaluate_test_evidence(out)
        assert ev.verdict == "fail_open"

    def test_error_de_compilacion_es_fail(self):
        out = (
            "Error: Couldn't resolve the package 'foo' from package:foo/foo.dart\n"
            "lib/src/a.dart:3:8: Error: Getter not found, 'bar'.\n"
        )
        ev = _ev().evaluate_test_evidence(out)
        assert ev.verdict == "fail"

    def test_no_sdk_es_fail_open(self):
        out = "flutter: command not found\n"
        ev = _ev().evaluate_test_evidence(out)
        assert ev.verdict == "fail_open"

    def test_pub_no_resuelve_deps_es_fail_open(self):
        out = (
            "Because app depends on foo ^1.0.0 which doesn't match any versions, "
            "version solving failed.\n"
        )
        ev = _ev().evaluate_test_evidence(out)
        assert ev.verdict == "fail_open"

    def test_startup_lock_es_fail_open(self):
        out = "Waiting for another flutter command to release the startup lock...\n"
        ev = _ev().evaluate_test_evidence(out)
        assert ev.verdict == "fail_open"


# =============================================================================
# flutter/dart analyze: "No issues found!" / "N issues found."
# =============================================================================
class TestAnalyzeEvidence:
    def test_no_issues(self):
        out = "Analyzing mtcg_mobile...\nNo issues found! (ran in 3.2s)\n"
        ev = _ev().evaluate_lint_evidence(out)
        assert ev.verdict == "pass"
        assert "limpio" in ev.reason

    def test_issues_encontrados(self):
        out = (
            "Analyzing mtcg_mobile...\n"
            "  warning • unused variable 'x' • lib/a.dart:3:9 • unused_local_variable\n"
            "  error • undefined method 'foo' • lib/b.dart:12:5 • undefined_method\n"
            "  2 issues found. (ran in 4.1s)\n"
        )
        ev = _ev().evaluate_lint_evidence(out)
        assert ev.verdict == "fail"
        assert "2" in ev.reason

    def test_un_error_found(self):
        out = (
            "Analyzing mtcg_mobile...\n"
            "  error • bad URI • lib/a.dart:1:8 • uri_does_not_exist\n"
            "1 error found.\n"
        )
        ev = _ev().evaluate_lint_evidence(out)
        assert ev.verdict == "fail"

    def test_zero_count_guard(self):
        out = "Analyzing app...\nNo issues found!\n"
        ev = _ev().evaluate_lint_evidence(out)
        assert ev.verdict == "pass"
