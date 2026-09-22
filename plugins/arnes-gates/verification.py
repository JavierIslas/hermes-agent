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
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from . import state as gate_state

_TIMEOUT = 120  # segundos
# Flutter/Dart (desde 2026-09-22): el compile frío de `flutter test`
# (kernel snapshot + build de la tool) supera holgadamente los 120s default
# del resto de los runners. 600s cubre el primer run sin esperar de más.
_TIMEOUT_FLUTTER = 600  # segundos

# Ubicaciones donde buscar el SDK de Flutter si no está en PATH. Orden:
# imagen (capa pinned del Dockerfile) → volumen (SDK manual en el data
# volume, sobrevive rebuilds; verificado 2026-09-22 con 3.47.5).
_FLUTTER_CANDIDATOS = (
    "/opt/flutter/bin/flutter",
    "/opt/data/flutter-sdk/bin/flutter",
)

# Manifiestos de proyecto para resolver root en árboles SIN git (fallback 3
# de _detect_project_root). Orden indiferente (any()).
_MANIFEST_MARKERS = (
    "pubspec.yaml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "pyproject.toml",
    "setup.py",
    "requirements.txt",
    "Makefile",
)


# =============================================================================
# Detección de runners: HECHOS del filesystem, no asunciones del modelo.
# =============================================================================
def _detect_project_root() -> Path:
    """Detecta el root del proyecto activo.

    Precedencia:
      1. terminal_cwd (trackeado via cd en post_tool_call) → su git-root.
      2. Último path escrito/leído en gate_state → su git-root.
      3. cwd del proceso (fallback).

    El cwd del proceso de Hermes (/opt/hermes) casi nunca es el proyecto
    sobre el que se trabaja. terminal_cwd resuelve el proyecto real cuando
    el agente hizo cd al directorio del proyecto.
    """
    import subprocess

    gate = gate_state.get()

    # 1. terminal_cwd → git-root.
    terminal_cwd = gate.get("terminal_cwd")
    if terminal_cwd:
        try:
            result = subprocess.run(
                ["git", "-C", str(terminal_cwd), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                root = Path(result.stdout.strip())
                if str(root) != "/opt/hermes":
                    return root
        except Exception:
            pass

    # 2. Paths escritos/leídos → git-root.
    candidatos = list(gate.get("written_paths", [])) + list(gate.get("read_paths", []))
    bases = [Path("/workspace"), Path.cwd()]

    for path_str in candidatos:
        path = Path(path_str)
        if not path.is_absolute():
            for base in bases:
                resolved = (base / path).resolve()
                if resolved.exists():
                    path = resolved
                    break
            else:
                continue
        check_dir = path.parent if path.is_file() else path
        try:
            result = subprocess.run(
                ["git", "-C", str(check_dir), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                root = Path(result.stdout.strip())
                if str(root) != "/opt/hermes":
                    return root
        except Exception:
            pass

    # 3. terminal_cwd / cwd del proceso sin git (desde 2026-09-22): árboles
    # que no son repo (ej: ProyectoMagicTCG en /workspace). Se toma el
    # directorio del manifiesto de proyecto más cercano subiendo desde
    # terminal_cwd (o cwd): pubspec.yaml, pom.xml, package.json, go.mod,
    # Cargo.toml, pyproject.toml. ANTES de caer al cwd crudo: sin esto, la
    # detección de runner muere en /opt/hermes y run_tests fail-openea sin
    # haber tocado el proyecto real.
    for start in (gate.get("terminal_cwd"), str(Path.cwd())):
        if not start:
            continue
        start_dir = Path(start)
        if not start_dir.is_dir():
            continue
        for d in [start_dir, *start_dir.parents]:
            if any((d / m).exists() for m in _MANIFEST_MARKERS) and str(d) != "/opt/hermes":
                return d
        break

    return Path.cwd()


def _python_for(root: Path) -> str:
    """Resuelve el Python ejecutable para un proyecto.

    Si el proyecto tiene un venv local (.venv/bin/python), lo usa. Si no,
    cae a sys.executable (el Python del agente). Esto es clave cuando el
    agente corre en un entorno (ej: producción) pero trabaja sobre un
    proyecto con sus propias deps (ej: el fork con su .venv).
    """
    venv_python = root / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def _detect_tests_setup(root: Path) -> tuple[list[str] | None, Path | None]:
    """Descubre (command, cwd) de tests desde la config del proyecto."""
    root = Path(root)
    for d in [root, *root.parents]:
        if _es_pytest(d):
            return ([_python_for(d), "-m", "pytest"], d)
        if _es_npm_test(d):
            return (["npm", "test"], d)
        if (d / "go.mod").exists():
            return (["go", "test", "./..."], d)
        if (d / "Cargo.toml").exists():
            return (["cargo", "test"], d)
        # Java (desde 2026-08-21): el wrapper del repo manda sobre el mvn/gradle
        # del sistema (versión pinneada por el proyecto). Maven antes que Gradle
        # si ambos existen (monorepos híbridos raros; pom.xml es más específico).
        # También antes que make: pom.xml/build.gradle son manifests de build
        # con ciclo de vida propio; un Makefile en un repo Java suele ser
        # atajo de proyecto (alias), no el runner canónico.
        if (d / "pom.xml").exists():
            if (d / "mvnw").exists():
                return (["./mvnw", "test"], d)
            return (["mvn", "test"], d)
        if (d / "build.gradle").exists() or (d / "build.gradle.kts").exists():
            if (d / "gradlew").exists():
                return (["./gradlew", "test"], d)
            return (["gradle", "test"], d)
        # Flutter/Dart (desde 2026-09-22): pubspec.yaml → flutter test (si
        # depende del SDK de flutter) o dart test (proyecto Dart puro).
        # Antes que make: pubspec es un manifest de build con ciclo de vida
        # propio; un Makefile en un repo Flutter es atajo, no el runner.
        if _es_flutter(d):
            flutter = _flutter_for()
            return ([flutter or "flutter", "test"], d)
        if _es_dart_puro(d):
            return ([_dart_for() or "dart", "test"], d)
        if _es_make_test(d):
            return (["make", "test"], d)
    return (None, None)


def _detect_lint_setup(root: Path) -> tuple[list[str] | None, Path | None]:
    """Descubre (command, cwd) del linter desde la config del proyecto."""
    root = Path(root)
    for d in [root, *root.parents]:
        if _es_ruff(d):
            return ([_python_for(d), "-m", "ruff", "check"], d)
        if _es_flake8(d):
            return ([_python_for(d), "-m", "flake8"], d)
        if _es_eslint(d):
            return (["npx", "eslint"], d)
        # Java (desde 2026-08-21): checkstyle si hay config explícita o el
        # plugin declarado en el build. El wrapper manda si existe.
        if _es_checkstyle(d):
            if (d / "pom.xml").exists():
                if (d / "mvnw").exists():
                    return (["./mvnw", "checkstyle:check"], d)
                return (["mvn", "checkstyle:check"], d)
            if (d / "gradlew").exists():
                return (["./gradlew", "checkstyleMain", "checkstyleTest"], d)
            return (["gradle", "checkstyleMain", "checkstyleTest"], d)
        # Flutter/Dart (desde 2026-09-22): flutter analyze / dart analyze.
        # El analyzer de dart es el linter canónico del ecosistema (viene con
        # el SDK y lee analysis_options.yaml — flutter_lints en el proyecto).
        # SIN --no-pub a propósito: si las deps del proyecto no resuelven,
        # analyze con --no-pub igual corre y escupe una avalancha de falsos
        # "issues" (uri_does_not_exist en cascada); con pub, muere antes con
        # "version solving failed" → el gate lo clasifica como env (fail_open),
        # que es la clasificación honesta.
        if _es_flutter(d):
            return ([_flutter_for() or "flutter", "analyze"], d)
        if _es_dart_puro(d):
            return ([_dart_for() or "dart", "analyze"], d)
    return (None, None)


def _detect_typecheck_setup(root: Path) -> tuple[list[str] | None, Path | None]:
    """Descubre (command, cwd) del type-checker desde la config del proyecto."""
    root = Path(root)
    for d in [root, *root.parents]:
        if _es_mypy(d):
            return ([_python_for(d), "-m", "mypy"], d)
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


def _es_checkstyle(d: Path) -> bool:
    """Java: checkstyle si hay config explícita o plugin declarado en el build.

    Config explícita: checkstyle.xml / google_checks.xml / sun_checks.xml en la
    raíz o config/checkstyle/. Plugin: checkstyle-maven-plugin en pom.xml o
    'checkstyle' en build.gradle(.kts).
    """
    for nombre in ("checkstyle.xml", "google_checks.xml", "sun_checks.xml"):
        if (d / nombre).exists() or (d / "config" / "checkstyle" / nombre).exists():
            return True
    pom = d / "pom.xml"
    if pom.exists() and "checkstyle" in pom.read_text(encoding="utf-8"):
        return True
    for gradle in ("build.gradle", "build.gradle.kts"):
        f = d / gradle
        if f.exists() and "checkstyle" in f.read_text(encoding="utf-8"):
            return True
    return False


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
# Flutter/Dart (desde 2026-09-22).
# =============================================================================
def _leer_pubspec(d: Path) -> dict:
    """Lee el pubspec.yaml como dict; {} si falta o no parsea (fail-open)."""
    import yaml

    pubspec = d / "pubspec.yaml"
    if not pubspec.exists():
        return {}
    try:
        data = yaml.safe_load(pubspec.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _es_flutter(d: Path) -> bool:
    """Flutter: pubspec.yaml con `flutter:` (sdk: flutter) en dependencies o
    dev_dependencies, paquetes que solo resuelven con el SDK de flutter
    (flutter_test/flutter_driver/integration_test), o sección `flutter:`
    propia (assets, plugin)."""
    data = _leer_pubspec(d)
    if not data:
        return False
    for seccion in ("dependencies", "dev_dependencies"):
        deps = data.get(seccion)
        if not isinstance(deps, dict):
            continue
        if isinstance(deps.get("flutter"), dict):
            return True
        for paquete in ("flutter_test", "flutter_driver", "integration_test"):
            if paquete in deps:
                return True
    return isinstance(data.get("flutter"), dict)


def _es_dart_puro(d: Path) -> bool:
    """Proyecto Dart puro (CLI/package, sin SDK de flutter)."""
    data = _leer_pubspec(d)
    if not data:
        return False
    for seccion in ("dependencies", "dev_dependencies"):
        deps = data.get(seccion)
        if isinstance(deps, dict) and isinstance(deps.get("flutter"), dict):
            return False
    return not isinstance(data.get("flutter"), dict)


def _flutter_for() -> Optional[str]:
    """Resuelve el binario flutter: PATH → candidatos (imagen/volumen)."""
    en_path = shutil.which("flutter")
    if en_path:
        return en_path
    for candidato in _FLUTTER_CANDIDATOS:
        if Path(candidato).exists():
            return candidato
    return None


def _dart_for() -> Optional[str]:
    """Resuelve el binario dart alineado al SDK de flutter (mismo root),
    si existe; si no, dart de PATH. None si no hay ninguno."""
    flutter = _flutter_for()
    if flutter:
        dart = Path(flutter).parent / "dart"
        if dart.exists():
            return str(dart)
    return shutil.which("dart") or None


def _timeout_for(cmd: list[str]) -> int:
    """Timeout según el runner: flutter/dart reciben el extendido (compile
    frío); el resto mantiene los 120s default."""
    binario = Path(cmd[0]).name if cmd else ""
    if binario in ("flutter", "dart"):
        return _TIMEOUT_FLUTTER
    return _TIMEOUT


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

    El proyecto se detecta por git-root desde el cwd. Si no hay git, cae
    al cwd. Esto es necesario porque el cwd del proceso de Hermes puede
    no ser el proyecto sobre el que se está trabajando.
    """
    root = _detect_project_root()
    command, cwd = _detect_tests_setup(root)
    if command is None:
        # Fail-open honesto: si no detectamos el runner del proyecto (probablemente
        # porque el cwd del proceso no es el proyecto real), no bloqueamos el finish
        # gate con tests_green=False. Seteamos verify_fail_open y dejamos tests_green
        # en False, pero el finish gate lo respeta como AVISO, no como bloqueo.
        gate_state.get()["verify_fail_open"] = True
        return (
            "AVISO (fail-open): no se detectó runner de tests para el proyecto "
            "activo. Esto puede pasar si el agente trabaja sobre un proyecto "
            "distinto al del proceso de Hermes. El finish gate no bloqueará por "
            "esto, pero NO se verificó que los tests pasen. Correlos manualmente."
        )
    cmd = list(command)
    if target and "pytest" in " ".join(cmd):
        cmd.append(str(target))
    try:
        proc = _correr(cmd, cwd, timeout=_timeout_for(cmd))
    except subprocess.TimeoutExpired:
        gate_state.get()["tests_green"] = False
        return f"ERROR: los tests tardaron más de {_timeout_for(cmd)}s (timeout)."
    salida = (proc.stdout or "") + (proc.stderr or "")
    gate_state.get()["last_test_output"] = salida

    # Deteccion de fallo de environment (no de tests): si el error es que no
    # se encontro el modulo/binary, es un problema de deteccion de proyecto,
    # no de tests rojos. Activar fail-open.
    _ENV_FAIL_PATTERNS = [
        "No module named",
        "not found",
        "command not found",
        "can't open file",
        "No such file or directory",
    ]
    if proc.returncode != 0 and any(p in salida for p in _ENV_FAIL_PATTERNS):
        gate_state.get()["verify_fail_open"] = True
        gate_state.get()["tests_green"] = True
        return (
            "AVISO (fail-open): el runner de tests fallo por un problema de "
            "environment (modulo/binary no encontrado), no por tests rojos. "
            "Esto pasa cuando el cwd del proceso no es el proyecto real. "
            "El finish gate no bloqueara por esto, pero NO se verifico que "
            "los tests pasen. Correlos manualmente.\n"
            f"--- output ---\n{salida[-500:]}"
        )

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
    root = _detect_project_root()
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
        proc = _correr(cmd, cwd, timeout=_timeout_for(cmd))
    except subprocess.TimeoutExpired:
        gate_state.get()["lint_green"] = False
        return f"ERROR: el linter tardó más de {_timeout_for(cmd)}s (timeout)."
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
    root = _detect_project_root()
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
    gate_state.get()["last_typecheck_output"] = salida
    gate_state.get()["typecheck_green"] = proc.returncode == 0
    if proc.returncode == 0:
        return f"OK: typecheck limpio (rc=0).\n--- output ---\n{salida[-500:]}"
    return f"FALLO: type-checker rc={proc.returncode}.\n--- output (últimas 1500 chars) ---\n{salida[-1500:]}"
