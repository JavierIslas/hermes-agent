"""Java language adapter — Nivel 1: regex + brace matching.

Sin tree-sitter ni javalang: el adapter extrae package/imports/clases/métodos
con expresiones regulares y balance de llaves. Suficiente para que
find_references/search_semantic vean el código Java y el índice no muerda
bytecode generado (DIRS_RUIDO += target/build/out/.gradle/.idea).

Límites honestos (Nivel 1):
- struct_hash = None siempre: Q3/Q4 (duplicación) degradan a AVISO para Java.
- check_syntax solo verifica balance de llaves: javac es el gate real de
  sintaxis (run_tests corre la compilación).
- Imports de JDK/terceros (java.*, com.fasterxml.*) no son verificables: el
  dangling-check de find_dangling es Python-only y no consume este adapter.
- Métodos anotados y generics en firmas se preservan tal cual (best effort).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Optional

from .lang_adapter import LanguageAdapter

# package com.x.ws;
_RE_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)

# import java.util.List; / import static java.util.Objects.requireNonNull;
_RE_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+(?:\.\*)?)\s*;", re.MULTILINE)

# Declaraciones de tipo: class/interface/enum/record (con modifiers y generics).
_RE_TYPE_DECL = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*"  # anotaciones sueltas
    r"(?:(?:public|protected|private|static|final|abstract|sealed|non-sealed|strictfp)\s+)*"
    r"(?:class|interface|enum|record)\s+(\w+)",
    re.MULTILINE,
)

# Declaraciones de método dentro de un cuerpo: modifiers + tipo de retorno +
# nombre + params. El brace matching desde la "{" define end_lineno.
_RE_METHOD_DECL = re.compile(
    r"^[ \t]*"
    r"(?:@\w+(?:\([^)]*\))?\s*(?:\n[ \t]*@\w+(?:\([^)]*\))?\s*)*)?"  # anotaciones
    r"(?:(?:public|protected|private|static|final|abstract|synchronized|native|default|strictfp)\s+)*"
    r"(?:<[\w\s,<>\[\].?]+>\s+)?"  # generics del método
    r"([\w<>\[\].?,\s]+?)\s+"  # tipo de retorno
    r"(\w+)\s*\(",  # nombre + apertura de parámetros
    re.MULTILINE,
)

# Identificadores para extract_usages (mismo espíritu que Python).
_RE_IDENT = re.compile(r"\b[A-Za-z_]\w*\b")

# Palabras clave que nunca son "usos".
_KEYWORDS = frozenset(
    """
    abstract assert boolean break byte case catch char class const continue
    default do double else enum extends final finally float for goto if
    implements import instanceof int interface long native new package private
    protected public return short static strictfp super switch synchronized
    this throw throws transient try var void volatile while yield record
    sealed permits non-sealed true false null
    """.split()
)

# Keywords de control: nunca pueden ser el "tipo de retorno" de una declaración
# real. Si el regex matchea "if (x) {" como método, el group(1) cae acá.
# Las primitivas (void/int/boolean/...) NO están: sí son tipos de retorno.
_CONTROL_KEYWORDS = frozenset(
    """
    break case catch continue default do else finally for goto if implements
    import instanceof new package permits return super switch this throw
    throws try while yield
    """.split()
)


def _strip_comments_strings(source: str) -> str:
    """Reemplaza comentarios y strings por espacios (mismo layout de líneas).

    Necesario antes de buscar declaraciones: "// class Foo" en un comentario
    no es una declaración, y "{ ... }" en un string rompe el brace matching.
    Conserva newlines para que los linenos sigan siendo válidos.
    """
    out: list[str] = []
    i = 0
    n = len(source)
    in_block = False
    in_line = False
    in_str: str | None = None  # '"' | "'"
    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if in_block:
            if c == "*" and nxt == "/":
                out.append("  ")
                i += 2
                in_block = False
            else:
                out.append("\n" if c == "\n" else " ")
                i += 1
        elif in_line:
            if c == "\n":
                out.append("\n")
                in_line = False
            else:
                out.append(" ")
            i += 1
        elif in_str is not None:
            if c == "\\":
                out.append("  ")
                i += 2
            elif c == in_str:
                out.append(" ")
                in_str = None
                i += 1
            else:
                out.append("\n" if c == "\n" else " ")
                i += 1
        else:
            if c == "/" and nxt == "/":
                in_line = True
                out.append("  ")
                i += 2
            elif c == "/" and nxt == "*":
                in_block = True
                out.append("  ")
                i += 2
            elif c in ('"', "'"):
                in_str = c
                out.append(" ")
                i += 1
            else:
                out.append(c)
                i += 1
    return "".join(out)


def _find_matching_brace(clean: str, open_idx: int) -> int | None:
    """Índice de la '}' que cierra la '{' en open_idx, o None si desbalanceado."""
    depth = 0
    for i in range(open_idx, len(clean)):
        c = clean[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _lineno_of(source: str, idx: int) -> int:
    return source.count("\n", 0, idx) + 1


class JavaAdapter(LanguageAdapter):
    """Adapter para Java via regex + brace matching (Nivel 1)."""

    @property
    def name(self) -> str:
        return "java"

    @property
    def extensions(self) -> frozenset[str]:
        return frozenset({".java"})

    def parse_source(self, source: str, rel: str) -> Optional[dict[str, Any]]:
        clean = _strip_comments_strings(source)

        m = _RE_PACKAGE.search(clean)
        package = m.group(1) if m else ""
        imports = sorted({m.group(1) for m in _RE_IMPORT.finditer(clean)})

        module = f"{package}.{Path(rel).stem}" if package else Path(rel).stem

        symbols: list[dict[str, Any]] = []
        # Pila de tipos abiertos (clases anidadas): (nombre, qualname, open_idx)
        stack: list[tuple[str, str, int]] = []

        for m in _RE_TYPE_DECL.finditer(clean):
            name = m.group(1)
            prefix = stack[-1][1] + "." if stack else ""
            qualname = f"{prefix}{name}"

            # Buscar la "{" del cuerpo de la clase a partir del fin de la decl.
            brace_idx = clean.find("{", m.end())
            if brace_idx == -1:
                continue  # declaración sin cuerpo (raro en .java válido)
            end_idx = _find_matching_brace(clean, brace_idx)
            if end_idx is None:
                return None  # llaves desbalanceadas: archivo roto, no indexar

            lineno = _lineno_of(source, m.start())
            end_lineno = _lineno_of(source, end_idx)
            symbols.append(
                {
                    "name": name,
                    "kind": "class",
                    "qualname": qualname,
                    "signature": f"{name}()",
                    "lineno": lineno,
                    "end_lineno": end_lineno,
                    "params": [],
                    "returns": None,
                    "struct_hash": None,
                }
            )

            # Métodos: solo dentro del cuerpo de ESTA clase (regiones ordenadas).
            body = (brace_idx, end_idx)
            self._collect_methods(clean, source, body, qualname, symbols)

            # Mantener stack para clases anidadas: cerrar tipos que ya pasamos.
            while stack and stack[-1][2] < m.start():
                stack.pop()
            stack.append((name, qualname, end_idx))

        return {
            "module": module,
            "symbols": symbols,
            "imports": imports,
            "hash": __import__("hashlib").sha256(source.encode("utf-8")).hexdigest(),
        }

    @staticmethod
    def _brace_depth_at(body_start: int, clean: str, idx: int) -> int:
        """Profundidad de llaves en idx, relativa al "{" de la clase (start).

        Escanea desde el propio "{" de la clase: miembros directos = 1,
        statements dentro de un método = 2. Relativo (no absoluto al archivo)
        para que las clases anidadas funcionen igual.
        """
        depth = 0
        for i in range(body_start, idx):
            c = clean[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
        return depth

    def _collect_methods(
        self,
        clean: str,
        source: str,
        body: tuple[int, int],
        class_qualname: str,
        out: list[dict[str, Any]],
    ) -> None:
        start, end = body
        for m in _RE_METHOD_DECL.finditer(clean, start, end):
            ret_type, name = m.group(1).strip(), m.group(2)
            if name in _KEYWORDS:
                continue
            # ret_type puede ser una primitiva (void/int/boolean...) — son
            # tipos de retorno VÁLIDOS en declaraciones. Lo que sí descarta
            # un falso método: que el "tipo" sea una keyword de control
            # (if/for/while/switch/catch) — esas nunca preceden a una decl.
            if ret_type in _CONTROL_KEYWORDS:
                continue
            # Filtro anti falsos positivos: una DECLARACIÓN de método vive a
            # profundidad de llaves 1 (miembro directo de la clase). Una LLAMADA
            # (assertEquals(x, y)) matchea el mismo regex pero vive dentro de
            # otro cuerpo, a profundidad >= 2. Sin esto, "foo(a, b) {" del
            # cuerpo de un método se indexa como método fantasma.
            if self._brace_depth_at(start, clean, m.start()) != 1:
                continue
            # La "(" ya la consumió el regex: buscar params hasta ")" y la "{".
            paren_close = clean.find(")", m.end(), end)
            brace_idx = clean.find("{", m.end(), end)
            if brace_idx == -1 or paren_close == -1 or paren_close > brace_idx:
                # Método abstracto/nativo sin cuerpo, o statement de control
                # (if/for/while/switch/try) matcheado como método.
                continue
            end_idx = _find_matching_brace(clean, brace_idx)
            if end_idx is None or end_idx > end:
                continue
            # Firmas al estilo Java: "ret name(params)". throws se omite.
            params_text = clean[m.end() : paren_close].strip()
            signature = f"{ret_type} {name}({params_text})".strip()
            out.append(
                {
                    "name": name,
                    "kind": "method",
                    "qualname": f"{class_qualname}.{name}",
                    "signature": signature,
                    "lineno": _lineno_of(source, m.start()),
                    "end_lineno": _lineno_of(source, end_idx),
                    "params": [
                        {"name": p.strip().split()[-1], "type": None}
                        for p in params_text.split(",")
                        if p.strip()
                    ],
                    "returns": ret_type or None,
                    "struct_hash": None,
                }
            )

    def check_syntax(self, source: str, path: str) -> Optional[str]:
        clean = _strip_comments_strings(source)
        depth = 0
        for c in clean:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth < 0:
                    return "error de sintaxis Java: '}' sin abrir (llaves desbalanceadas)"
        if depth != 0:
            return (
                f"error de sintaxis Java: {depth} llave(s) sin cerrar "
                "(llaves desbalanceadas)"
            )
        return None

    def extract_usages(self, content: str) -> set[str]:
        return {t for t in _RE_IDENT.findall(content) if t not in _KEYWORDS}


# Auto-registro: cubre el import directo de lang_java (sin pasar por
# lang_adapter primero). Si lang_adapter nos está importando a nosotros,
# su guard de re-entrancia ya manejó el ciclo y "java" estará (o no) en el
# registry cuando este módulo termine de cargar; el check de membresía evita
# el doble registro.
def _self_register() -> None:
    from . import lang_adapter as _la

    if "java" not in _la._registry:
        _la.register(JavaAdapter())


_self_register()
