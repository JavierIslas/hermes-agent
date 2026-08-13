"""Gates basados en evidencia: el finish gate evalúa OUTPUT CRUDO, no booleans.

El finish gate ya NO checkea `tests_green`/`lint_green`/`typecheck_green`
(booleans mutables que el modelo puede falsificar via terminal). En cambio,
evalúa `last_test_output`/`last_lint_output` (strings crudos del resultado
de correr el runner) y devuelve un verdict.

Verdicts:
  "pass"      — hay output reciente con N passed > 0 y rc=0.
  "fail"      — hay output con fallos reales (tests rojos, lint errors).
  "fail_open" — no hay output, o el output muestra fallo de environment
                (No module found, etc.). No se verificó, pero no se bloquea.

Modelo de amenaza:
  El modelo puede falsificar last_test_output escribiendo un string falso via
  terminal. Pero el string tiene que ser realista (conteos, paths, timings) y
  es auditable en el log. Esto es órdenes de magnitud más difícil que flipear
  un boolean. La garantía total requiere Fase 4 (core surgery).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Evidence:
    """Verdict del análisis de output crudo de un runner."""
    verdict: str  # "pass" | "fail" | "fail_open"
    reason: str


# =============================================================================
# Patrones de detección en output crudo.
# =============================================================================

# pytest: "37 passed" o "37 passed, 2 failed"
_RE_PYTEST_PASSED = re.compile(r"(\d+)\s+passed")
_RE_PYTEST_FAILED = re.compile(r"(\d+)\s+failed")
_RE_PYTEST_ERROR = re.compile(r"=\s*ERROR\s")  # pytest collection error
_RE_PYTEST_NO_TESTS = re.compile(r"no tests ran")

# Fallos de environment (no de tests): el runner no pudo arrancar.
_ENV_FAIL_PATTERNS = [
    "No module named",
    "command not found",
    "not found",
    "can't open file",
    "No such file or directory",
    "ImportError",
    "ModuleNotFoundError",
    "Permission denied",
]

# Linters: ruff/flake8/mypy
_RE_RUFF_CLEAN = re.compile(r"All checks passed|Found 0 errors")
_RE_RUFF_ERRORS = re.compile(r"Found (\d+) error")
_RE_GENERIC_LINT_ERRORS = re.compile(r"(\d+)\s+(error|warning|violation)", re.IGNORECASE)
_RE_MYPY_OK = re.compile(r"Success: no issues found|Found 0 errors")
_RE_MYPY_ERRORS = re.compile(r"Found (\d+) error")


# =============================================================================
# Evaluadores.
# =============================================================================

def evaluate_test_evidence(output: Optional[str]) -> Evidence:
    """Evalúa el output crudo de run_tests y devuelve un verdict."""
    if not output or not output.strip():
        return Evidence("fail_open", "no hay output de tests — no se verificaron")

    # ¿Fallo de environment?
    for pattern in _ENV_FAIL_PATTERNS:
        if pattern in output:
            return Evidence(
                "fail_open",
                f"fallo de environment detectado ('{pattern}'). "
                "El runner no pudo arrancar — no es un fallo de tests. "
                "El finish gate no bloquea, pero NO se verificaron los tests."
            )

    # ¿Tests pasaron?
    passed_match = _RE_PYTEST_PASSED.search(output)
    if passed_match:
        n_passed = int(passed_match.group(1))
        if n_passed > 0:
            # ¿También hubo fallos?
            failed_match = _RE_PYTEST_FAILED.search(output)
            if failed_match and int(failed_match.group(1)) > 0:
                return Evidence(
                    "fail",
                    f"{n_passed} passed pero {failed_match.group(1)} failed. "
                    "Corregí los tests que fallan antes de cerrar."
                )
            return Evidence("pass", f"{n_passed} tests pasaron (rc=0)")

    # ¿Tests fallaron sin passed?
    if _RE_PYTEST_FAILED.search(output):
        failed_match = _RE_PYTEST_FAILED.search(output)
        return Evidence(
            "fail",
            f"{failed_match.group(1)} tests fallaron. "
            "Corregí los tests antes de cerrar."
        )

    # ¿Error de colección de pytest?
    if _RE_PYTEST_ERROR.search(output):
        return Evidence("fail", "pytest tuvo un error de colección. Revisá el output.")

    # ¿No corrió ningún test?
    if _RE_PYTEST_NO_TESTS.search(output):
        return Evidence(
            "fail_open",
            "no se corrió ningún test (0 tests). El finish gate no bloquea, "
            "pero NO se verificó nada. Considerá escribir tests."
        )

    # Output no reconocido: fail-open honesto.
    return Evidence(
        "fail_open",
        "output de tests no reconocido. No se pudo verificar automáticamente. "
        "El finish gate no bloquea, pero revisá el output manualmente."
    )


def evaluate_lint_evidence(output: Optional[str]) -> Evidence:
    """Evalúa el output crudo de run_lint y devuelve un verdict."""
    if not output or not output.strip():
        return Evidence("fail_open", "no hay output de lint — no se verificó")

    # ¿Fallo de environment?
    for pattern in _ENV_FAIL_PATTERNS:
        if pattern in output:
            return Evidence(
                "fail_open",
                f"fallo de environment detectado ('{pattern}'). "
                "El linter no pudo arrancar. El finish gate no bloquea."
            )

    # ¿Lint limpio?
    if _RE_RUFF_CLEAN.search(output) or _RE_MYPY_OK.search(output):
        return Evidence("pass", "linter limpio (0 errores)")

    # ¿Errores de lint?
    ruff_errors = _RE_RUFF_ERRORS.search(output)
    if ruff_errors:
        return Evidence(
            "fail",
            f"linter encontró {ruff_errors.group(1)} errores. "
            "Corregí los errores de lint antes de cerrar."
        )

    mypy_errors = _RE_MYPY_ERRORS.search(output)
    if mypy_errors:
        return Evidence(
            "fail",
            f"type-checker encontró {mypy_errors.group(1)} errores. "
            "Corregí los errores de tipo antes de cerrar."
        )

    generic = _RE_GENERIC_LINT_ERRORS.search(output)
    if generic:
        return Evidence(
            "fail",
            f"linter encontró {generic.group(1)} {generic.group(2)}s. "
            "Corregí antes de cerrar."
        )

    # Output no reconocido: si está vacío o no tiene errores, asumir pass.
    # Muchos linters no imprimen nada cuando todo está limpio.
    return Evidence("pass", "linter sin output de errores (asumido limpio)")


def evaluate_typecheck_evidence(output: Optional[str]) -> Evidence:
    """Evalúa el output crudo de run_typecheck y devuelve un verdict."""
    if not output or not output.strip():
        return Evidence("fail_open", "no hay output de typecheck — no se verificó")

    # ¿Fallo de environment?
    for pattern in _ENV_FAIL_PATTERNS:
        if pattern in output:
            return Evidence(
                "fail_open",
                f"fallo de environment detectado ('{pattern}'). "
                "El type-checker no pudo arrancar. El finish gate no bloquea."
            )

    # ¿Typecheck limpio?
    if _RE_MYPY_OK.search(output):
        return Evidence("pass", "type-checker limpio (0 errores)")

    # ¿Errores de tipo?
    mypy_errors = _RE_MYPY_ERRORS.search(output)
    if mypy_errors:
        return Evidence(
            "fail",
            f"type-checker encontró {mypy_errors.group(1)} errores. "
            "Corregí los errores de tipo antes de cerrar."
        )

    # Output no reconocido.
    return Evidence(
        "fail_open",
        "output de typecheck no reconocido. El finish gate no bloquea."
    )
