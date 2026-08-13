"""Tests de evidence.py: gates basados en evidencia (output crudo)."""
import sys
import types
import importlib.util
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent / "arnes-gates"


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
    from hermes_plugins.arnes_gates.evidence import (
        evaluate_test_evidence,
        evaluate_lint_evidence,
        evaluate_typecheck_evidence,
    )
    return evaluate_test_evidence, evaluate_lint_evidence, evaluate_typecheck_evidence


# =============================================================================
# evaluate_test_evidence.
# =============================================================================
def test_pass_pytest():
    ev_test, _, _ = _ev()
    e = ev_test("=== 37 passed in 0.85s ===")
    assert e.verdict == "pass"
    assert "37" in e.reason


def test_fail_with_failures():
    ev_test, _, _ = _ev()
    e = ev_test("=== 35 passed, 2 failed in 1.2s ===")
    assert e.verdict == "fail"
    assert "2" in e.reason


def test_fail_only_failures():
    ev_test, _, _ = _ev()
    e = ev_test("=== 3 failed in 0.5s ===")
    assert e.verdict == "fail"


def test_fail_open_empty():
    ev_test, _, _ = _ev()
    e = ev_test(None)
    assert e.verdict == "fail_open"


def test_fail_open_no_module():
    ev_test, _, _ = _ev()
    e = ev_test("No module named pytest")
    assert e.verdict == "fail_open"
    assert "environment" in e.reason.lower()


def test_fail_open_command_not_found():
    ev_test, _, _ = _ev()
    e = ev_test("bash: pytest: command not found")
    assert e.verdict == "fail_open"


def test_fail_open_no_tests_ran():
    ev_test, _, _ = _ev()
    e = ev_test("no tests ran in 0.00s")
    assert e.verdict == "fail_open"


def test_fail_collection_error():
    ev_test, _, _ = _ev()
    e = ev_test("=== ERROR collecting tests/test_foo.py ===")
    assert e.verdict == "fail"


def test_fail_open_unrecognized():
    ev_test, _, _ = _ev()
    e = ev_test("some random output that doesn't match anything")
    assert e.verdict == "fail_open"


# =============================================================================
# evaluate_lint_evidence.
# =============================================================================
def test_lint_pass_ruff():
    _, ev_lint, _ = _ev()
    e = ev_lint("All checks passed!")
    assert e.verdict == "pass"


def test_lint_fail_ruff():
    _, ev_lint, _ = _ev()
    e = ev_lint("Found 5 errors in 3 files.")
    assert e.verdict == "fail"
    assert "5" in e.reason


def test_lint_fail_open_empty():
    _, ev_lint, _ = _ev()
    e = ev_lint(None)
    assert e.verdict == "fail_open"


def test_lint_fail_open_no_module():
    _, ev_lint, _ = _ev()
    e = ev_lint("No module named ruff")
    assert e.verdict == "fail_open"


def test_lint_pass_empty_output():
    """Muchos linters no imprimen nada cuando todo está limpio."""
    _, ev_lint, _ = _ev()
    e = ev_lint("")
    assert e.verdict == "fail_open"  # empty → fail_open (no hay nada que evaluar)


# =============================================================================
# evaluate_typecheck_evidence.
# =============================================================================
def test_typecheck_pass_mypy():
    _, _, ev_tc = _ev()
    e = ev_tc("Success: no issues found in 15 source files")
    assert e.verdict == "pass"


def test_typecheck_fail_mypy():
    _, _, ev_tc = _ev()
    e = ev_tc("Found 3 errors in 2 files (checked 15 source files)")
    assert e.verdict == "fail"
    assert "3" in e.reason


def test_typecheck_fail_open_empty():
    _, _, ev_tc = _ev()
    e = ev_tc(None)
    assert e.verdict == "fail_open"


def test_typecheck_fail_open_no_module():
    _, _, ev_tc = _ev()
    e = ev_tc("No module named mypy")
    assert e.verdict == "fail_open"


# =============================================================================
# Falsificación — el modelo intenta fakear evidence.
# =============================================================================
def test_faked_pytest_output_still_passes():
    """El modelo puede escribir un falso output. Evidence lo acepta como pass.
    Esto es por diseño: es auditable en el log. La garantía total requiere Fase 4.
    """
    ev_test, _, _ = _ev()
    e = ev_test("=== 100 passed in 0.01s ===")
    assert e.verdict == "pass"  # aceptamos el riesgo
