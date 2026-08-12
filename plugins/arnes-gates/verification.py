"""Tools de verificación: run_tests, run_lint, run_typecheck.

Port simplificado de refs/agent_juguete/tools/verification.py. La lógica
central: detectar el runner del filesystem (pytest, ruff, mypy), correrlo
con subprocess, y setear el flag de gate según el exit code.

Adaptaciones vs el original:
- Sin manifest.toml: detección pura del filesystem.
- Sin _resolve/_dentro_del_root del agent_juguete: uso pathlib + cwd.
- Sin setup_tests/setup_lint/setup_typecheck: el modelo puede crear la
  config via write_file si hace falta (respeta los gates).
- Detección recorre hacia arriba desde cwd (root, parent, ...).

Las tres tools prenden flags en gate_state:
  run_tests     → tests_green
  run_lint      → lint_green
  run_typecheck → typecheck_green
El finish gate (pre_verify) exige los tres verdes.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from . import state as gate_state

_TIMEOUT = 120  # segundos


# =============================================================================
# Detección de runners: HECHOS del filesystem, no asunciones del modelo.
# =============================================================================
def _detect_tests_setup(root: Path) -> tuple[list[str] | None, Path | None]:
    """Descubre (command, cwd) de tests desde la config del proyecto."""
    root = Path(root)
    for d in [root, *root.parents]:
        if _es_pytest(d):
            return ([sys.executable, "-m", "pytest"], d)
        if _es_npm_test(d):
            return (["npm", "test"], d)
        if (d / "go.mod").exists():
            return (["go", "test", "./..."], d)
        if (d / "Cargo.toml").exists():
            return (["cargo", "test"], d)
        if _es_make_test(d):
            return (["make", "test"], d)
    return (None, None)


def _detect_lint_setup(root: Path) -> tuple[list[str] | None, Path | None]:
    """Descubre (command, cwd) del linter desde la config del proyecto."""
    root = Path(root)
    for d in [root, *root.parents]:
        if _es_ruff(d):
            return ([sys.executable, "-m", "ruff", "check"], d)
        if _es_flake8(d):
            return ([sys.executable, "-m", "flake8"], d)
        if _es_eslint(d):
            return (["npx", "eslint"], d)
    return (None, None)


def _detect_typecheck_setup(root: Path) -> tuple[list[str] | None, Path | None]:
    """Descubre (command, cwd) del type-checker desde la config del proyecto."""
    root = Path(root)
    for d in [root, *root.parents]:
        if _es_mypy(d):
            return ([sys.executable, "-m", "mypy"], d)
    return (None, None)


# =============================================================================
# Detectores de config por herramienta.
# =============================================================================
def _es_pytest(d: Path) -> bool:
    if (d / "pytest.ini").exists():
        return True
    tox = d / "tox.ini"
    if tox.exists() and "[pytest]" in tox.read_text(encoding="utf-8"):
        return True
    setup = d / "setup.cfg"
    if setup.exists() and "[tool:pytest]" in setup.read_text(encoding="utf-8"):
        return True
    pyproject = d / "pyproject.toml"
    if pyproject.exists() and "[tool.pytest" in pyproject.read_text(encoding="utf-8"):
        return True
    return False


def _es_npm_test(d: Path) -> bool:
    pkg = d / "package.json"
    if not pkg.exists():
        return False
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return bool(data.get("scripts", {}).get("test"))


def _es_make_test(d: Path) -> bool:
    mk = d / "Makefile"
    if not mk.exists():
        return False
    return any(linea.startswith("test:") for linea in mk.read_text(encoding="utf-8").splitlines())


def _es_ruff(d: Path) -> bool:
    if (d / "ruff.toml").exists() or (d / ".ruff.toml").exists():
        return True
    pyproject = d / "pyproject.toml"
    return pyproject.exists() and "[tool.ruff" in pyproject.read_text(encoding="utf-8")


def _es_flake8(d: Path) -> bool:
    if (d / ".flake8").exists():
        return True
    for nombre in ("setup.cfg", "tox.ini"):
        f = d / nombre
        if f.exists() and "[flake8]" in f.read_text(encoding="utf-8"):
            return True
    return False


def _es_eslint(d: Path) -> bool:
    return any(d.glob(".eslintrc*")) or any(d.glob("eslint.config.*"))


def _es_mypy(d: Path) -> bool:
    if (d / "mypy.ini").exists() or (d / ".mypy.ini").exists():
        return True
    for nombre in ("pyproject.toml", "setup.cfg"):
        f = d / nombre
        if f.exists():
            texto = f.read_text(encoding="utf-8")
            if "[tool.mypy]" in texto or "[mypy]" in texto:
                return True
    return False


# =============================================================================
# Helpers de ejecución.
# =============================================================================
def _correr(cmd: list[str], cwd: Path | None, timeout: int = _TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


_PYTEST_PASSED_RE = re.compile(r"(\d+)\s+passed")


def _contar_pasados_pytest(salida: str) -> int:
    coincidencia = _PYTEST_PASSED_RE.search(salida)
    return int(coincidencia.group(1)) if coincidencia else 0


# =============================================================================
# Tools: run_tests, run_lint, run_typecheck.
# =============================================================================
def run_tests(target: Optional[str] = None) -> str:
    """Corre los tests del proyecto y prende tests_green si pasan (rc==0).

    El runner se detecta del filesystem (pytest, npm, go, cargo, make).
    target (opcional) es un path que se le pasa al runner.
    """
    root = Path.cwd()
    command, cwd = _detect_tests_setup(root)
    if command is None:
        return (
            "ERROR: no se detectó runner de tests en el repo. Si el proyecto "
            "usa pytest, asegurate de que haya un pytest.ini/pyproject.toml con "
            "[tool.pytest] o instalá pytest. Si usás otro runner, declaralo."
        )
    cmd = list(command)
    if target and "pytest" in " ".join(cmd):
        cmd.append(str(target))
    try:
        proc = _correr(cmd, cwd)
    except subprocess.TimeoutExpired:
        gate_state.get()["tests_green"] = False
        return f"ERROR: los tests tardaron más de {_TIMEOUT}s (timeout)."
    salida = (proc.stdout or "") + (proc.stderr or "")
    gate_state.get()["last_test_output"] = salida
    if proc.returncode == 0 and "pytest" in " ".join(cmd):
        if _contar_pasados_pytest(salida) == 0:
            gate_state.get()["tests_green"] = False
            return (
                f"AVISO: rc=0 pero 0 tests pasaron. 0 tests no cuenta como "
                f"verde.\n--- output ---\n{salida[-500:]}"
            )
    gate_state.get()["tests_green"] = proc.returncode == 0
    if proc.returncode == 0:
        return f"OK: tests pasaron (rc=0).\n--- output ---\n{salida[-500:]}"
    return f"FALLO: rc={proc.returncode}.\n--- output (últimas 1500 chars) ---\n{salida[-1500:]}"


def run_lint(target: Optional[str] = None) -> str:
    """Corre el linter del proyecto y prende lint_green si pasa (rc==0).

    El linter se detecta del filesystem (ruff, flake8, eslint).
    """
    root = Path.cwd()
    command, cwd = _detect_lint_setup(root)
    if command is None:
        return (
            "ERROR: no se detectó linter en el repo. Si el proyecto usa ruff, "
            "asegurate de que haya ruff.toml o [tool.ruff] en pyproject.toml."
        )
    cmd = list(command)
    if target:
        cmd.append(str(target))
    else:
        cmd.append(str(root))
    try:
        proc = _correr(cmd, cwd)
    except subprocess.TimeoutExpired:
        gate_state.get()["lint_green"] = False
        return f"ERROR: el linter tardó más de {_TIMEOUT}s (timeout)."
    salida = (proc.stdout or "") + (proc.stderr or "")
    gate_state.get()["last_lint_output"] = salida
    gate_state.get()["lint_green"] = proc.returncode == 0
    if proc.returncode == 0:
        return f"OK: linter limpio (rc=0).\n--- output ---\n{salida[-500:]}"
    return f"FALLO: linter rc={proc.returncode}.\n--- output (últimas 1500 chars) ---\n{salida[-1500:]}"


def run_typecheck(target: Optional[str] = None) -> str:
    """Corre el type-checker del proyecto y prende typecheck_green si pasa.

    El type-checker se detecta del filesystem (mypy).
    Si no hay type-checker declarado, devuelve AVISO (no bloquea — es opt-in).
    """
    root = Path.cwd()
    command, cwd = _detect_typecheck_setup(root)
    if command is None:
        # typecheck es opt-in: si no hay type-checker, no se exige.
        gate_state.get()["typecheck_green"] = True
        return (
            "AVISO: no se detectó type-checker (mypy) en el repo. "
            "Typecheck skip (opt-in: si declarás mypy, finish lo exigirá)."
        )
    cmd = list(command)
    if target:
        cmd.append(str(target))
    else:
        cmd.append(str(root))
    try:
        proc = _correr(cmd, cwd)
    except subprocess.TimeoutExpired:
        gate_state.get()["typecheck_green"] = False
        return f"ERROR: el type-checker tardó más de {_TIMEOUT}s (timeout)."
    salida = (proc.stdout or "") + (proc.stderr or "")
    gate_state.get()["typecheck_green"] = proc.returncode == 0
    if proc.returncode == 0:
        return f"OK: typecheck limpio (rc=0).\n--- output ---\n{salida[-500:]}"
    return f"FALLO: type-checker rc={proc.returncode}.\n--- output (últimas 1500 chars) ---\n{salida[-1500:]}"
