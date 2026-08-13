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
from pathlib import Path
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

# git commit: comando que materializa cambios en git.
_RE_GIT_COMMIT = re.compile(r"(^|\s|\|)\bgit\s+commit\b")


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


def detect_git_commit(cmd: str) -> bool:
    """True si el comando contiene `git commit` (materializacion en git)."""
    if not cmd:
        return False
    return bool(_RE_GIT_COMMIT.search(cmd))


def _safe_tokenize(cmd: str) -> list[str]:
    """Tokeniza el comando con shlex, fallando-open a split simple."""
    try:
        return shlex.split(cmd, posix=True)
    except ValueError:
        # Comando con quotes desbalanceados: fallback a split por espacios.
        return cmd.split()


# =============================================================================
# extract_write_targets: extrae los paths de destino de un comando que escribe.
# =============================================================================
def extract_write_targets(cmd: str) -> list[str]:
    """Extrae los paths de destino (lo que se escribe) de un comando shell.

    Devuelve una lista de paths (pueden ser relativos). Solo cubre los
    patrones que detecta should_block — no es un parser shell completo.

    Casos cubiertos:
      echo x > foo.py           → ["foo.py"]
      echo x >> foo.py          → ["foo.py"]
      cat <<EOF > foo.py        → ["foo.py"]
      cp src.py dst.py          → ["dst.py"]
      mv old.py new.py          → ["new.py"]
      sed -i 's/a/b/' f.py      → ["f.py"]
      dd of=file.bin            → ["file.bin"]
      tee foo.py                → ["foo.py"]
      echo x | tee a.py b.py    → ["a.py", "b.py"]
    """
    if not cmd or not cmd.strip():
        return []

    tokens = _safe_tokenize(cmd)
    targets: list[str] = []

    # 1. Redireccion: el token despues de > o >> es el destino.
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == ">" or tok == ">>":
            if i + 1 < len(tokens):
                targets.append(tokens[i + 1])
                i += 2
                continue
        elif tok.startswith(">") and len(tok) > 1 and tok[1] not in (">", "&"):
            # >archivo pegado al operador.
            targets.append(tok[1:])
        elif tok.startswith(">>") and len(tok) > 2:
            targets.append(tok[2:])
        i += 1

    # 2. Heredoc: el destino es lo que sigue al > despues del <<MARKER.
    if _RE_HEREDOC.search(cmd):
        # Ya cubierto por la redireccion si hay > despues del heredoc.
        # Si no hay > (heredoc a stdout), no hay archivo destino.
        pass

    # 3. sed -i: el archivo a editar es el ultimo argumento.
    if _RE_SED_INPLACE.search(cmd):
        # sed -i 'expr' file.py → file.py es el ultimo token no-flag.
        file_args = _extract_sed_target(tokens)
        targets.extend(file_args)

    # 4. dd of=ARCHIVO.
    dd_match = re.search(r"\bof=(\S+)", cmd)
    if dd_match:
        targets.append(dd_match.group(1))

    # 5. cp/mv: el destino es el ultimo argumento.
    if _RE_CP.search(cmd) or _RE_MV.search(cmd):
        dest = _extract_cp_mv_dest(tokens)
        if dest:
            targets.append(dest)

    # 6. tee: todos los argumentos son destinos (tee a.py b.py).
    if _RE_TEE.search(cmd):
        tee_args = _extract_tee_targets(tokens)
        targets.extend(tee_args)

    # 7. install: el destino es el ultimo argumento (igual que cp).
    if _RE_INSTALL.search(cmd):
        dest = _extract_cp_mv_dest(tokens)
        if dest:
            targets.append(dest)

    # Dedup preservando orden.
    seen: set[str] = set()
    result: list[str] = []
    for t in targets:
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


# --- Helpers de extract_write_targets ---

_FLAGS_SED = {"-i", "-n", "-e", "-f", "--in-place", "--quiet", "--expression", "--file"}


def _extract_sed_target(tokens: list[str]) -> list[str]:
    """Extrae el archivo target de sed -i 'expr' file."""
    result: list[str] = []
    skip_next = False
    for i, tok in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if tok in ("-e", "-f", "--expression", "--file"):
            skip_next = True
            continue
        if tok in _FLAGS_SED or tok.startswith("-"):
            continue
        if tok == "sed":
            continue
        # El primer argumento no-flag despues de -i es la expresion.
        # Los siguientes son archivos.
        result.append(tok)
    # El primer elemento es la expresion sed, no un archivo. Sacarlo.
    if result:
        result.pop(0)
    return result


def _extract_cp_mv_dest(tokens: list[str]) -> Optional[str]:
    """Extrae el destino de cp/mv (ultimo argumento no-flag)."""
    args: list[str] = []
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok in ("-r", "-R", "-f", "-v", "-p", "-i", "--recursive", "--force"):
            continue
        if tok.startswith("-") and len(tok) > 1:
            # Flag con valor pegado o flag largo: skip.
            continue
        if tok in ("cp", "mv", "install"):
            continue
        args.append(tok)
    if len(args) >= 2:
        return args[-1]  # cp src dest → dest
    return None


def _extract_tee_targets(tokens: list[str]) -> list[str]:
    """Extrae los archivos destino de tee.

    Solo procesa lo que está despues del token 'tee' — ignorando lo
    que viene del pipe (echo x | tee foo.py → solo foo.py).
    """
    result: list[str] = []
    # Encontrar donde empieza tee.
    try:
        tee_idx = tokens.index("tee")
    except ValueError:
        return []
    skip_next = False
    for tok in tokens[tee_idx + 1:]:
        if skip_next:
            skip_next = False
            continue
        if tok in ("-a", "--append", "-i", "--ignore-interrupts"):
            continue
        if tok.startswith("-") and len(tok) > 1:
            continue
        result.append(tok)
    return result


# =============================================================================
# parse_cd: extrae el nuevo cwd despues de un comando con cd.
# =============================================================================
def parse_cd(cmd: str, current_cwd: str | None = None) -> str | None:
    """Parsea un comando shell y devuelve el nuevo cwd si contiene cd.

    Devuelve None si el comando no contiene cd (el cwd no cambia).

    Casos:
      cd /workspace/foo           → /workspace/foo
      cd ../bar                   → resolver relativo a current_cwd
      cd                          → $HOME (fallback: None, no se puede resolver)
      cd "path con espacios"      → path con espacios
      cd subdir && pytest         → subdir (cd al inicio de comando compuesto)
      cd -                        → None (no podemos saber el cwd anterior)

    Limitacion: cd dentro de subshell (cd foo && ...) dentro de ( ) no
    cambia el cwd del terminal padre. Eso es correcto.
    """
    if not cmd or not cmd.strip():
        return None

    tokens = _safe_tokenize(cmd)

    # Buscar el primer cd en el comando (cd a principio de comando o
    # despues de && o ;).
    for i, tok in enumerate(tokens):
        if tok != "cd":
            continue
        # cd sin argumentos → $HOME. No podemos resolverlo sin el entorno.
        if i + 1 >= len(tokens) or tokens[i + 1] in ("&&", ";", "|"):
            return None  # cd sin args → $HOME, no podemos resolver
        target = tokens[i + 1]
        # Limpiar ; o && pegados al target (shlex los deja pegados).
        target = target.rstrip(";")
        # cd - → directorio anterior, no podemos saberlo.
        if target == "-":
            return None
        # Resolver path.
        if target.startswith("/"):
            return target
        if current_cwd:
            return str(Path(current_cwd) / target)
        # Sin current_cwd y path relativo → no podemos resolver.
        return None

    return None
