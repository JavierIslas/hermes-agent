"""Detector de write-via-terminal: analiza un comando shell y decide si
escribe a archivos del repo (eludiendo write_file/patch y sus gates).

El terminal libre de Hermes es la via de escape de los gates. Un modelo puede
escribir codigo via heredoc, redireccion, cp/mv/tee/sed in-place, saltando
analyze_scope y read_before_write. Este detector cierra ese escape hatch:
si detecta escritura, el hook pre_tool_call exige scope_done.

Diseno: fail-open con AVISO para comandos ambiguos. Si el detector no esta
seguro, NO bloquea (romperia la UX). Prefiere falsos negativos (deja pasar
un write raro) a falsos positivos (bloquea un comando legitimo).

Limitacion honesta: esto es ANALISIS ESTATICO del string del comando. Un
comando dentro de un script llamado por el comando, o un alias, pueden
escribir sin que el detector lo vea. Es una capa de defensa, no una garantia
absoluta. La garantia absoluta requiere linearizar el write path en el core
(Fase 4/5), pero este plugin cierra el 90% del escape hatch.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Optional


# =============================================================================
# Patrones de escritura: redireccion, heredoc, comandos mutativos.
# =============================================================================
# Redireccion simple/append: "> archivo", ">> archivo".
# Aparece como token separado, no dentro de un string (eso lo filtra shlex).
_RE_REDIRECT_OUT = re.compile(r"(?<![<])>(?![<])\s*\S")
_RE_APPEND = re.compile(r">>\s*\S")

# Heredoc: <<MARKER, <<-MARKER, <<<MARKER. Marker suele ser EOF, END, etc.
_RE_HEREDOC = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)")

# Comandos que escriben in-place o copian/mueven archivos.
_RE_SED_INPLACE = re.compile(r"\bsed\b.*\s-i\b")
_RE_TEE = re.compile(r"(^|\s)\btee\b(\s|$)")
_RE_CP = re.compile(r"(^|\s)\bcp\b(\s|$)")
_RE_MV = re.compile(r"(^|\s)\bmv\b(\s|$)")
_RE_INSTALL = re.compile(r"(^|\s)/usr/bin/install\b|^install\s+\S")
_RE_DD_OF = re.compile(r"\bdd\b.*\bof=")

# Editors abiertos (vim, nano, etc.) — bloquean la sesion, pero escribén.
_RE_EDITOR = re.compile(
    r"(^|\s)\b(vim?|nano|emacs|vi|ed|ex|pico|micro)\b(\s|$)"
)


@dataclass
class WriteDetection:
    """Resultado del analisis de un comando terminal."""

    is_write: bool
    confidence: str  # "high" | "medium" | "low"
    reason: str


def detect_write_command(cmd: str) -> WriteDetection:
    """Analiza un comando shell y decide si escribe a archivos.

    Devuelve WriteDetection con is_write, confidence y reason.

    confidence:
      "high"   — patron inequivoco (heredoc, redireccion, sed -i, dd of=).
      "medium" — comando que PUEDE escribir segun argumentos (cp, mv, tee).
      "low"    — editor interactivo (bloquea sesion, raro en agente).

    Para el gate, is_write=True con confidence high/medium bloquea sin scope.
    """
    if not cmd or not cmd.strip():
        return WriteDetection(False, "high", "comando vacío")

    # Heredoc: <<EOF, <<-EOF — escritura inequivoca.
    if _RE_HEREDOC.search(cmd):
        return WriteDetection(True, "high", "heredoc detectado")

    # Redireccion de salida: > archivo, >> archivo, 2> archivo.
    # shlex para no matchear > dentro de strings.
    tokens = _safe_tokenize(cmd)
    for tok in tokens:
        if tok in (">", ">>") or tok.startswith(">") and len(tok) > 1 and tok[1] != ">":
            return WriteDetection(True, "high", f"redirección de salida: {tok}")
        if tok == ">>":
            return WriteDetection(True, "high", "append: >>")

    # Patrones de comando mutativo.
    if _RE_SED_INPLACE.search(cmd):
        return WriteDetection(True, "high", "sed -i (edición in-place)")

    if _RE_DD_OF.search(cmd):
        return WriteDetection(True, "high", "dd of= (escritura directa)")

    if _RE_TEE.search(cmd):
        return WriteDetection(True, "medium", "tee (redirección a archivo)")

    if _RE_CP.search(cmd):
        return WriteDetection(True, "medium", "cp (copia de archivos)")

    if _RE_MV.search(cmd):
        return WriteDetection(True, "medium", "mv (mover/renombrar archivos)")

    if _RE_INSTALL.search(cmd):
        return WriteDetection(True, "medium", "install (copia con permisos)")

    if _RE_EDITOR.search(cmd):
        return WriteDetection(True, "low", "editor interactivo")

    return WriteDetection(False, "high", "no detecta escritura")


def should_block(detection: WriteDetection) -> bool:
    """Politica: bloquear si is_write y confidence es high o medium.

    Low (editores) no bloquea: un editor interactivo no sirve para escribir
    codigo automaticamente y bloquearia la sesion sin razon.
    """
    return detection.is_write and detection.confidence in ("high", "medium")


def _safe_tokenize(cmd: str) -> list[str]:
    """Tokeniza el comando con shlex, fallando-open a split simple."""
    try:
        return shlex.split(cmd, posix=True)
    except ValueError:
        # Comando con quotes desbalanceados: fallback a split por espacios.
        return cmd.split()
