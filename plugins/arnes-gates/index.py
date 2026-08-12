"""Índice estructural del codebase (discover v0.2).

Fuente de verdad de HECHOS del repo: qué símbolos se definen (con firma) y qué
importa cada archivo. Lo construye `run()` al arrancar (bootstrap, como el
manifest) y lo mantiene `write_file` incremental al escribir. Lo consultan los
gates de hechos futuros (firma citada) y tools (find_references) para verificar
contra el MUNDO REAL, no contra lo que el modelo cree haber visto.

Distinción clave (igual que manifest.py): el índice es DATO con esquema que las
tools/gates leen, NO texto inyectado al prompt. La regla vive en el gate que
consulta el índice. Si el índice mintiera (símbolo inexistente o firma falsa),
convertiría al gate en info-falsa-con-autoridad — la peor falla (lección 2). Por
eso `load_index` valida frescura y `_parse_file` no inventa: parsea o saltea.

Alcance: símbolos (función/clase + firma + params tipados + returns + qualname)
+ grafo de imports + content-hashing granular (v0.3: cada entry lleva `hash`+`mtime`;
`reconcile` da cache hit por archivo al arrancar — solo reparsa lo que cambió) +
params/returns tipados vía ast (v0.4: para el gate de firma citada). NO incluye
architecture propuesta por LLM (mezcla hecho+interpretación).

Hechos (determinísticos, de `ast`): `files[path] -> {module, symbols[], imports[],
hash, mtime}` donde cada símbolo es `{name, kind, qualname, signature, lineno,
params: [{name, type}], returns, struct_hash}` (v0.5: struct_hash = sha256 de la
forma canónica de cada def, para detección de duplicación; None si trivial <
MIN_NODOS_DUP). Derivados en memoria (no persistidos): reverse
index `{símbolo: [paths]}` y `{módulo: [paths]}` — son estado derivable; duplicarlos
en disco sería fuente de drift.
"""

import ast
import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Iterator

INDEX_VERSION = 5  # v0.6: símbolos con end_lineno (chunking para embeddings)

# Complejidad mínima (nodos del body) para considerar una def como duplicable.
# MIN=5 (agresivo): detecta hasta micro-funciones (x*x, a+b son isomorfas, ~5-7
# nodos); acepta el tradeoff de bloquear adders/wrappers triviales legítimos.
# Subir MIN pierde duplicados cortos; el sweet spot no existe para ese par.
MIN_NODOS_DUP = 5

# Nodo def/clase (unión para anotaciones — los 3 tienen .name, .lineno, .body, .args).
_DefNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef

# Directorios que no aportan al codebase: ruido o generados. Compartido con
# retrieval.py (grep/find_references lo usan al recorrer archivos) — una sola
# fuente para no desincronizar lo que se ignora al indexar/buscar. Sin underscore:
# es constante pública entre módulos del paquete (retrieval la importa).
DIRS_RUIDO = {
    "__pycache__",
    ".git",
    ".venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
}


# =============================================================================
# Parser de un archivo .py -> {module, symbols, imports}.
# =============================================================================
def _t(annotation: ast.expr | None) -> str | None:
    """Tipo de una anotación ast como string, o None si no hay anotación.

    Usa ast.unparse (determinístico, fiel al source). Forward-refs como string
    (x: "Foo") quedan CON comillas ('Foo') — signatures_match las normaliza.
    """
    return ast.unparse(annotation) if annotation is not None else None


def _p(arg: ast.arg) -> dict[str, Any]:
    """Dict de un parámetro: {name, type}. type=None si el parámetro no se anotó."""
    return {"name": arg.arg, "type": _t(arg.annotation)}


def _params(args: ast.arguments) -> list[dict[str, Any]]:
    """Lista de parámetros de una firma como dicts {name, type}.

    Mantiene el orden y los marcadores *args/**kw (prefijados) para que el gate
    de firma citada pueda comparar aridad y nombres. El type de *args/**kw es del
    ELEMENTO (vía ast), no del contenedor — caveat: semánticamente tuple[int, ...].
    """
    out = [_p(a) for a in args.posonlyargs]
    out += [_p(a) for a in args.args]
    if args.vararg:
        out.append({"name": "*" + args.vararg.arg, "type": _t(args.vararg.annotation)})
    out += [_p(a) for a in args.kwonlyargs]
    if args.kwarg:
        out.append({"name": "**" + args.kwarg.arg, "type": _t(args.kwarg.annotation)})
    return out


# =============================================================================
# Normalización estructural: forma canónica de una def para detectar duplicación.
# Anonimiza los nombres LOCALES (params + targets asignados en el scope) y preserva
# literales, llamadas externas y atributos. Así `f(x): x*x` ≡ `g(y): y*y`, pero
# `x*2` ≢ `x*3` y `cuadrado(x)` ≢ `doble(x)` (reusar vs duplicar).
# =============================================================================
def _strip_docstring(fn: ast.AST) -> None:
    """Saca el docstring (body[0] = Expr(Constant str)) de una def/clase in place."""
    body = getattr(fn, "body", None)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body.pop(0)


def _nombres_target(target: ast.expr) -> set[str]:
    """Nombres ligados por un target de asignación (Name, Tuple/List, Starred)."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        out = set()
        for e in target.elts:
            out |= _nombres_target(e)
        return out
    if isinstance(target, ast.Starred):
        return _nombres_target(target.value)
    return set()


def _walk_mismo_scope(node: ast.AST) -> Iterator[ast.AST]:
    """Yield nodos del scope de `node` SIN descender a defs/classes/lambda anidadas.

    Sus locals son de otro scope — no interesan para el contenedor.
    """
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield from _walk_mismo_scope(child)


def _collect_def_locals(fn: ast.AST) -> set[str]:
    """Nombres LOCALES del scope de fn: params + targets asignados en el body (sin
    descender a scopes anidados), menos los declarados global/nonlocal."""
    locales = set()
    args = getattr(fn, "args", None)
    if args is not None:
        for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            locales.add(a.arg)
        if args.vararg:
            locales.add(args.vararg.arg)
        if args.kwarg:
            locales.add(args.kwarg.arg)
    externos = set()
    for stmt in getattr(fn, "body", []) or []:
        for node in _walk_mismo_scope(stmt):
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                externos.update(node.names)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    locales |= _nombres_target(t)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                locales |= _nombres_target(node.target)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                locales |= _nombres_target(node.target)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        locales |= _nombres_target(item.optional_vars)
            elif isinstance(node, ast.ExceptHandler):
                if node.name:
                    locales.add(node.name)
            elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                for gen in node.generators:
                    locales |= _nombres_target(gen.target)
    return locales - externos


def _rename_locals(node: ast.AST, locales: set[str], mapping: dict[str, str]) -> None:
    """Renombra in place los Names/args locales por placeholders __L{k}__ (pre-order:
    primer contacto asigna el índice). No desciende a scopes anidados."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Name) and child.id in locales:
            child.id = mapping.setdefault(child.id, f"__L{len(mapping)}__")
        elif isinstance(child, ast.arg) and child.arg in locales:
            child.arg = mapping.setdefault(child.arg, f"__L{len(mapping)}__")
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            _rename_locals(child, locales, mapping)


def _contar_nodos_body(fn: ast.AST) -> int:
    """Cuenta nodos ast del body (proxy de complejidad para el umbral de duplicación)."""
    return sum(1 for stmt in getattr(fn, "body", []) or [] for _ in ast.walk(stmt))


def _normalizar_def(node: Any) -> tuple[str, int]:
    """Forma canónica de una def/clase para detección de duplicación estructural.

    Devuelve (canonical_str, node_count). canonical = ast.dump del árbol con:
    docstring quitado, nombre propio anulado, locales renombrados a placeholders.
    Literales, llamadas externas y atributos se PRESERVAN (distinguen reusar de
    duplicar). node_count = nodos del body (para el umbral MIN_NODOS_DUP).
    """
    n = copy.deepcopy(node)
    _strip_docstring(n)
    locales = _collect_def_locals(n) | {n.name}
    mapping: dict[str, str] = {}
    _rename_locals(n, locales, mapping)
    n.name = ""
    canonical = ast.dump(n, annotate_fields=False)
    return canonical, _contar_nodos_body(n)


def _symbol(node: Any, kind: str, qualname: str) -> dict[str, Any]:
    """Dict de un símbolo: {name, kind, qualname, signature, lineno, end_lineno,
    params, returns, struct_hash}.

    params = [{name, type}] (type=None si sin anotación); returns = str|None.
    signature incluye ' -> {ret}' solo si hay return annotation (firma completa
    y legible — la que muestran find_references al usuario). struct_hash = sha256
    de la forma canónica (None si la def es trivial, < MIN_NODOS_DUP) — para
    detección de duplicación estructural. end_lineno = línea final (ast) para
    slicear el cuerpo (chunking de embeddings).
    """
    name = node.name
    if kind == "function":
        signature = f"{name}({ast.unparse(node.args)})"
        returns = _t(node.returns)
        if returns is not None:
            signature += f" -> {returns}"
        params = _params(node.args)
    else:  # class: los params del constructor viven en __init__, no acá.
        signature = f"{name}()"
        params = []
        returns = None
    canonical, node_count = _normalizar_def(node)
    struct_hash = (
        hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if node_count >= MIN_NODOS_DUP
        else None
    )
    return {
        "name": name,
        "kind": kind,
        "qualname": qualname,
        "signature": signature,
        "lineno": node.lineno,
        "end_lineno": node.end_lineno,
        "params": params,
        "returns": returns,
        "struct_hash": struct_hash,
    }


def slice_source(source: str, lineno: int, end_lineno: int | None) -> str:
    """Subcadena de `source` entre `lineno` y `end_lineno` (inclusive, 1-indexado).

    `end_lineno` None -> solo la línea `lineno`. Base del chunking de embeddings: el
    cuerpo (con firma real, nombre y decoradores) vive en el source, no en el índice.
    """
    end = end_lineno if end_lineno is not None else lineno
    lineas = source.splitlines(keepends=True)
    return "".join(lineas[lineno - 1 : end])


def chunk_de_symbol(symbol: dict[str, Any], source: str) -> str:
    """Texto de un símbolo (su source slice) para embeber.

    Usa `lineno`/`end_lineno` del symbol del índice. La unidad de embedding es el
    cuerpo de la def/clase tal cual está en disco (la firma reconstruida del índice
    ya viene incluida en el slice, así que no se duplica).
    """
    return slice_source(source, symbol["lineno"], symbol.get("end_lineno"))


def _collect_symbols(node: ast.AST, prefix: str, out: list[dict[str, Any]]) -> None:
    """Recorre el AST recolectando defs, manteniendo el qualname jerárquico.

    ast.walk no sirve para símbolos: pierde el contexto del padre (no sabría que
    un método es Clase.metodo). Recorremos a mano bajando el prefijo. Las defs
    anidadas en if/for/with/try se capturan también (son pseudo-top-level).
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qual = f"{prefix}{child.name}" if prefix else child.name
            out.append(_symbol(child, "function", qual))
            _collect_symbols(child, qual + ".", out)
        elif isinstance(child, ast.ClassDef):
            qual = f"{prefix}{child.name}" if prefix else child.name
            out.append(_symbol(child, "class", qual))
            _collect_symbols(child, qual + ".", out)
        else:
            # Nodos no-def (if, for, with, try, assign) pueden contener defs
            # anidadas — seguimos bajando SIN extender el prefijo.
            _collect_symbols(child, prefix, out)


def _parse_source(source: str, rel: str, root: str | Path) -> dict[str, Any] | None:
    """Parsea el source (ya leído) de un .py -> {module, symbols, imports, hash} o None.

    None = syntax error. `hash` = sha256 del source (content-addressing: permite a
    `reconcile` decidir si reparsar sin volver a leer). No conoce el archivo (no
    hay `mtime` acá — lo pone `_parse_file`). Separada de `_parse_file` para que
    `reconcile` reutilice el `source` que ya leyó al hashear (sin doble read).
    """
    try:
        tree = ast.parse(source, filename=str(rel))
    except SyntaxError:
        return None
    symbols: list[dict[str, Any]] = []
    _collect_symbols(tree, "", symbols)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            # Relative imports sin módulo (from . import x) se pierden: caveat del
            # grafo de deps v0.2 — el contexto del paquete no se reconstruye.
            imports.add(node.module)
    module = ".".join(Path(rel).with_suffix("").parts)
    return {
        "module": module,
        "symbols": symbols,
        "imports": sorted(imports),
        "hash": hashlib.sha256(source.encode("utf-8")).hexdigest(),
    }


def _parse_file(abs_path: str | Path, root: str | Path) -> dict[str, Any] | None:
    """Lee un .py y lo parsea -> {module, symbols, imports, hash, mtime} o None.

    None = archivo binario/ilegible o syntax error. No inventamos símbolos:
    run_tests atrapará el syntax error igual; el índice simplemente no lo incluye.
    `mtime`+`hash` permiten a `reconcile` decidir si reparsar sin volver a leer.
    """
    path = Path(abs_path)
    try:
        source = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None
    entry = _parse_source(source, str(path.relative_to(root)), root)
    if entry is None:
        return None
    entry["mtime"] = path.stat().st_mtime
    return entry


_PARSEABLE_EXTS = {".py"}  # extensiones que el parser ast realmente maneja (capability)


def _lenguaje_soporta_parser(manifest: Any) -> bool:
    """True si el lenguaje declarado tiene parser estructural (hoy solo python).

    Sin manifest (None) o stack python -> True. Otro stack -> False: el índice
    queda vacío (no hay .py que parsear) y los gates estructurales degradan con
    AVISO. No se simula verificación que no se puede hacer — la stdlib no parsea
    JS/Go/Rust, y la regla del proyecto prohíbe dependencias nuevas para parsers.
    """
    if manifest is None:
        return True
    return getattr(manifest, "stack", "python") == "python"


def _es_indexable(path: Path | str, manifest: Any) -> bool:
    """True si el archivo calza para los gates estructurales: extensión parseable
    Y lenguaje con parser. python + .py -> True; resto -> False (gates se relajan)."""
    return Path(path).suffix in _PARSEABLE_EXTS and _lenguaje_soporta_parser(manifest)


def _iter_source_files(root: str | Path, exts: Iterable[str] = (".py",)) -> Iterator[Path]:
    """Yield archivos bajo root cuyos sufijos estén en exts, salteando DIRS_RUIDO."""
    sufijos = set(exts)
    for p in sorted(Path(root).rglob("*")):
        if p.is_file() and p.suffix in sufijos and not any(
            part in DIRS_RUIDO for part in p.parts
        ):
            yield p


def _iter_py_files(root: str | Path) -> Iterator[Path]:
    """Alias de _iter_source_files(('.py',)) — retrocompat para callers históricos."""
    return _iter_source_files(root, (".py",))


# =============================================================================
# build_index + reverse (hechos del repo).
# =============================================================================
def build_index(root: str | Path) -> dict[str, Any]:
    """Construye el índice de todos los .py bajo root. Determinístico."""
    root = Path(root)
    files = {}
    for p in _iter_py_files(root):
        entry = _parse_file(p, root)
        if entry is not None:
            files[str(p.relative_to(root))] = entry
    return {
        "version": INDEX_VERSION,
        "root": str(root.resolve()),
        "files": files,
    }


def reverse_by_symbol(index: dict[str, Any]) -> dict[str, list[str]]:
    """Deriva {nombre_o_qualname: [paths relativos]} — dónde se define cada símbolo."""
    out: dict[str, list[str]] = {}
    for path, entry in index["files"].items():
        for sym in entry["symbols"]:
            out.setdefault(sym["name"], []).append(path)
            if sym["qualname"] != sym["name"]:
                out.setdefault(sym["qualname"], []).append(path)
    return out


def reverse_by_import(index: dict[str, Any]) -> dict[str, list[str]]:
    """Deriva {módulo: [paths relativos]} — qué archivos importan cada módulo."""
    out: dict[str, list[str]] = {}
    for path, entry in index["files"].items():
        for mod in entry["imports"]:
            out.setdefault(mod, []).append(path)
    return out


# =============================================================================
# Dangling references: imports a módulos/símbolos del proyecto que ya no existen.
# Consumido por el gate en commit (gates.py). El índice guarda imports como módulos
# (pierde los names del `from X import a,b` — caveat _parse_source), así que para
# detectar símbolos renombrados hay que reparsar el importador y sacar los names.
# =============================================================================
def _imports_de(tree: ast.AST) -> list[tuple[str, list[str], int, int]]:
    """Extrae imports de un AST: [(modulo, names, level, lineno), ...].

    `ast.Import` -> un item por alias (módulo absoluto, names=[], level=0).
    `ast.ImportFrom` -> (node.module o '', [alias.name...], node.level). Los
    relativos sin módulo (`from . import x`, module=None) quedan con modulo=''.
    """
    items: list[tuple[str, list[str], int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                items.append((alias.name, [], 0, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            modulo = node.module or ""
            names = [alias.name for alias in node.names]
            items.append((modulo, names, node.level, node.lineno))
    return items


def _resolver_modulo(
    modulo: str, level: int, importer_module: str, root_package: str
) -> str | None:
    """Resuelve un import a su módulo target (dotted, relativo al root del índice).

    Absoluto (level=0): si `modulo` empieza con `{root_package}.` le saca ese prefijo
    (los entries del índice son módulos relativos al root); si es el propio root
    package -> '' (el __init__ del root). Si no arranca con el prefijo -> None
    (externo/stdlib: no verificable, el índice solo parsea .py del root).

    Relativo (level>0): sube `level-1` paquetes desde el package del importador y le
    appendea `modulo`. `from .mod import` (level=1) en `sub.imp` -> `sub.mod`;
    `from ..mod import` (level=2) -> `mod`. None si sube más allá del root.
    """
    if level > 0:
        partes = importer_module.split(".") if importer_module else []
        pkg = partes[:-1]  # package del importador (subir 1 del módulo al package)
        if level - 1 > len(pkg):
            return None
        base = pkg[: len(pkg) - (level - 1)]
        target = ".".join(base + (modulo.split(".") if modulo else []))
        return target
    prefijo = root_package + "."
    if modulo == root_package:
        return ""
    if modulo.startswith(prefijo):
        return modulo[len(prefijo) :]
    return None


def _nombres_definidos(tree: ast.AST) -> set[str]:
    """Nombres que un módulo VINCULA en su namespace (visibles para un importador):
    def/class, asignaciones a `Name` (constantes) y names de imports (re-exports).

    Un `from modulo import X` resuelve si X está acá o es un submódulo. Capturar
    asignaciones y re-exports es clave: el índice solo guarda def/class, así que sin
    esto cualquier `from X import CONSTANTE` (TOOLS, DIRS_RUIDO, state) daría falso
    dangling.
    """
    nombres = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nombres.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    nombres.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            nombres.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                nombres.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                nombres.add(alias.asname or alias.name.split(".")[0])
    return nombres


def _names_faltantes(
    names: list[str],
    target: str,
    nombres_def_por_mod: dict[str, set[str]],
    mods_definidos: set[str],
) -> list[str]:
    """De los names de un `from target import ...`, cuáles no están vinculados en target
    (ni como def/class/constante/re-export) ni son submódulo suyo. Esos son dangling."""
    definidos = nombres_def_por_mod.get(target)
    if definidos is None:
        # target es un package cuyo __init__ se indexa como "{target}.__init__": los
        # names públicos del package viven en su __init__.
        definidos = nombres_def_por_mod.get(f"{target}.__init__", set())
    faltan = []
    for nombre in names:
        if nombre in definidos:
            continue
        submod = f"{target}.{nombre}" if target else nombre
        if submod in mods_definidos:
            continue  # submódulo: from paquete import submod
        faltan.append(nombre)
    return faltan


def _existe_modulo(target: str, mods_definidos: set[str]) -> bool:
    """Si un módulo target existe en el índice. `target` directo, o como package: el
    índice computa `__init__.py` como module `{dir}.__init__` (no `{dir}`), así que un
    package válido (con __init__ o submódulos) también cuenta."""
    if target in mods_definidos:
        return True
    prefijo = f"{target}." if target else ""
    return any(
        m == f"{target}.__init__" or (prefijo and m.startswith(prefijo)) for m in mods_definidos
    )


def es_arranque(index: dict[str, Any]) -> bool:
    """True si el índice no tiene código verificable (repo vacío / solo config).

    Consulta PURA (input índice → bool) que run() usa como snapshot al inicio de cada
    corrida para decidir el MODO: si ningún archivo define símbolos (def/class), el
    repo está en FASE 0 — la fuente de verdad es una spec aprobada por el operador
    (write_spec/spec_done), no analyze_scope (no hay nada que scopear ni reutilizar).
    En cuanto hay al menos un símbolo, es repo con código y scope_done gobierna.

    Un repo con solo `__init__.py` vacío o asignaciones sueltas (sin def/class) cuenta
    como arranque: no hay lógica contra la cual verificar. Conservador por diseño.
    """
    for entry in index.get("files", {}).values():
        if entry.get("symbols"):
            return False
    return True


def find_dangling(index: dict[str, Any], root: str | Path) -> list[dict[str, Any]]:
    """Detecta imports colgantes: referencias a módulos/símbolos DEL PROYECTO que no
    existen (renombrados/borrados). Dos niveles:

    - Módulo: `import paquete.mod` / `from paquete.mod import x` donde `mod` no está en
      el índice (se borró/renombró el archivo).
    - Símbolo: `from paquete.mod import foo` donde `foo` no está vinculado en `mod`
      (def/class/constante/re-export) ni es submódulo suyo (se renombró/borró).

    Solo verifica imports DEL PROYECTO: absolutos con prefijo `{root_package}.` o
    relativos (level>0). Los externos/stdlib (os, openai) se ignoran — no están en el
    índice por diseño y no podemos verificarlos (falso negativo asumido).

    El índice guarda imports como módulos y símbolos solo def/class (pierde los names
    del `from X import a,b` y las constantes), así que reparsa cada archivo UNA vez:
    extrae sus imports (como importador) y los nombres que vincula (como target
    potencial de from-imports — def/class/constantes/re-exports). Patrón de
    `_buscar_usos` en retrieval.py; no es hot path (lo llama commit() al cerrar).

    Devuelve [{importer, lineno, modulo, tipo, faltantes}, ...] ordenado por
    (importer, lineno). `tipo` es 'modulo' o 'simbolo'; `faltantes` los nombres que no
    se resolvieron (para 'modulo', [el módulo target]).
    """
    root = Path(root)
    root_package = root.name
    mods_definidos = {entry["module"] for entry in index["files"].values()}
    # Una pasada por archivo: sus imports (importador) + los nombres que vincula (target).
    por_archivo = {}  # rel_path -> (tree, entry)
    nombres_def_por_mod = {}  # module -> set de nombres vinculados
    for importer_path, entry in index["files"].items():
        abs_path = root / importer_path
        try:
            tree = ast.parse(abs_path.read_text(encoding="utf-8"), filename=importer_path)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        por_archivo[importer_path] = (tree, entry)
        nombres_def_por_mod[entry["module"]] = _nombres_definidos(tree)

    dangling = []
    for importer_path, (tree, entry) in por_archivo.items():
        for modulo, names, level, lineno in _imports_de(tree):
            target: str | None
            if level == 0 and modulo in mods_definidos:
                # import absoluto a un módulo del proyecto SIN prefijo del root package
                # (estilo común cuando el root está en sys.path: `from src import X`).
                # _resolver_modulo lo descartaría como externo; acá lo reclamamos.
                target = modulo
            else:
                target = _resolver_modulo(modulo, level, entry["module"], root_package)
                if target is None:
                    continue  # externo / no verificable
            # target="" es el root package (from paquete import X): siempre existe.
            target_existe = target == "" or _existe_modulo(target, mods_definidos)
            if modulo and not target_existe:
                # import con módulo explícito que no existe -> se borró/renombró el archivo
                dangling.append(
                    {
                        "importer": importer_path,
                        "lineno": lineno,
                        "modulo": modulo,
                        "tipo": "modulo",
                        "faltantes": [target or "(root package)"],
                    }
                )
                continue
            if names:
                faltan = _names_faltantes(names, target, nombres_def_por_mod, mods_definidos)
                if faltan:
                    dangling.append(
                        {
                            "importer": importer_path,
                            "lineno": lineno,
                            "modulo": modulo or target,
                            "tipo": "simbolo",
                            "faltantes": faltan,
                        }
                    )
    dangling.sort(key=lambda d: (d["importer"], d["lineno"]))
    return dangling


# =============================================================================
# Firma citada: extraer la firma real de un símbolo y compararla con la citada.
# Consumido por el gate en analyze_scope (gates.py). El índice guarda ast.unparse
# crudo (fiel al source); la normalización indulgente vive acá, solo al comparar.
# =============================================================================
# Alias typing → builtin: solo nombres conocidos, por palabra (NO lowercasear todo
# — un MiClase custom se rompería). Tabla fija, no heurística.
_ALIAS_TIPO = {
    "List": "list",
    "Dict": "dict",
    "Set": "set",
    "FrozenSet": "frozenset",
    "Tuple": "tuple",
    "Type": "type",
}


def _norm_tipo(t: str | None) -> str | None:
    """Normalización indulgente de un string de tipo. None → None (no comparar).

    Aplica: strip + colapsar whitespace, quitar comillas de forward-ref ('Foo'→Foo),
    Optional[X]→X|None (un nivel; greedy para list[int]), alias mayúsculas→builtin
    (tabla). NO normaliza equivalencia semántica (list vs Sequence), orden de
    uniones, ni alias anidados profundos — escapa (documentado en el gate).
    """
    if t is None:
        return None
    s = re.sub(r"\s+", "", t.strip())
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1]  # forward-ref string: 'Foo' → Foo
    s = re.sub(r"Optional\[(.+)\]", r"\1|None", s)
    for mayus, minus in _ALIAS_TIPO.items():
        s = re.sub(rf"\b{mayus}\b", minus, s)
    return s


def _strip_self(params: list[dict[str, Any]], es_metodo: bool) -> list[dict[str, Any]]:
    """Saca self/cls del primer param cuando es método (qualname con punto).

    El índice guarda self (veraz); el modelo cita la firma de uso (sin self), como
    inspect.signature de un método bound. Solo para métodos: una función libre con
    un param literalmente 'self' NO se toca.
    """
    if es_metodo and params and params[0].get("name") in ("self", "cls"):
        return params[1:]
    return params


def extract_signature(name: str, index: dict[str, Any]) -> list[dict[str, Any]]:
    """Candidatos del índice para `name` (por nombre simple o qualname).

    Un símbolo puede definirse en varios archivos → devuelve todos. [] si no existe.
    Cada candidato: {path, name, qualname, params, returns} — lo que signatures_match
    necesita (qualname para el stripping de self; params/returns para comparar).
    """
    out = []
    for path, entry in index["files"].items():
        for sym in entry["symbols"]:
            if name in (sym["name"], sym["qualname"]):
                out.append(
                    {
                        "path": path,
                        "name": sym["name"],
                        "qualname": sym["qualname"],
                        "params": sym["params"],
                        "returns": sym["returns"],
                    }
                )
    return out


def signatures_match(cited: dict[str, Any], real: dict[str, Any]) -> bool:
    """True si la firma citada calza con la real (normalización indulgente).

    cited: {name, params:[{name, type?}], returns?}. real: candidato de
    extract_signature. Reglas:
    - self/cls: se saca de AMBOS lados si real es método (qualname con punto).
    - aridad: misma cuenta de params post-stripping.
    - por param: name SIEMPRE calza; type solo si el citado no es None (omitido
      → no se compara — el gate es fuerte sobre lo afirmado, no lo omitido).
    - return: solo si el citado no es None.
    """
    es_metodo = "." in real.get("qualname", "")
    cited_params = _strip_self(cited.get("params") or [], es_metodo)
    real_params = _strip_self(real.get("params") or [], es_metodo)
    if len(cited_params) != len(real_params):
        return False
    for citado, real_p in zip(cited_params, real_params, strict=True):
        if citado.get("name") != real_p.get("name"):
            return False
        tipo_citado = citado.get("type")
        if tipo_citado is not None and _norm_tipo(tipo_citado) != _norm_tipo(real_p.get("type")):
            return False
    ret_citado = cited.get("returns")
    if ret_citado is not None and _norm_tipo(ret_citado) != _norm_tipo(real.get("returns")):
        return False
    return True


def _nombres_referenciados(source: str) -> set[str]:
    """Set de nombres que el source REFERENCIA (uso, no definición).

    ast.Name (id) + ast.Attribute (attr). Distingue el símbolo de texto igual en
    comentarios (no los ve) y strings (ast.Constant, no Name): `def cuadrado` no
    genera un ast.Name → el gate distingue REDEFINIR de REUTILIZAR. Para el gate
    reuse-in-diff en write_file. SyntaxError → set vacío (write_file ya frenó el
    .py roto con check_syntax antes). Comparte el patrón 'walk Name/Attribute' con
    retrieval._buscar_usos, pero NO se unifican: firmas y propósito distintos (esta
    devuelve set de TODOS los nombres; _buscar_usos filtra por un símbolo y devuelve
    path→líneas). Extraer una abstracción intermedia para 2 usos sería premature;
    unificar recién a la 3ra aparición del patrón.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
    return out


# =============================================================================
# Detección de duplicación estructural (fila 5): capta lo omitido.
# =============================================================================
def _collect_def_nodes(tree: ast.AST) -> Iterator[_DefNode]:
    """Yield FunctionDef/AsyncFunctionDef/ClassDef de un árbol (top-level + anidados)."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node


def _reverse_by_struct_hash(index: dict[str, Any]) -> dict[str, list[tuple[str, str, str]]]:
    """{struct_hash: [(qualname, path, signature)]} — solo defs no triviales."""
    out: dict[str, list[tuple[str, str, str]]] = {}
    for path, entry in index["files"].items():
        for sym in entry["symbols"]:
            h = sym.get("struct_hash")
            if h is not None:
                out.setdefault(h, []).append((sym["qualname"], path, sym["signature"]))
    return out


def _buscar_duplicados(
    content: str,
    index: dict[str, Any] | None,
    rel_propio: str,
    excluir_nombres: frozenset[str] = frozenset(),
) -> list[tuple[str, str, str, str]]:
    """Defs del content que duplican estructuralmente una def existente del índice.

    Devuelve [(name_nueva, qualname_existente, path_existente, signature)].
    Excluye: defs citadas en `excluir_nombres` (comprometidas en reuse), la del
    propio archivo (`rel_propio` — reescribir tu def no es dup), y las triviales
    (< MIN_NODOS_DUP). [] si no hay índice o el content no parsea.
    """
    if index is None:
        return []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    rev = _reverse_by_struct_hash(index)
    out = []
    for def_node in _collect_def_nodes(tree):
        nombre = def_node.name.split(".")[-1]
        if nombre in excluir_nombres:
            continue
        canonical, ncount = _normalizar_def(def_node)
        if ncount < MIN_NODOS_DUP:
            continue
        h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        for qual, path, sig in rev.get(h, []):
            # Misma def reescrita (mismo path Y mismo nombre) → no es dup. Una def
            # DISTINTA en el mismo archivo con mismo cuerpo → SÍ es dup (ej: añadir
            # `area_cuadrado` a example.py que ya tiene `cuadrado` con mismo cuerpo).
            if path == rel_propio and qual.split(".")[-1] == def_node.name:
                continue
            out.append((def_node.name, qual, path, sig))
            break  # 1er match por def (no spamear múltiples)
    return out


# =============================================================================
# Persistencia + frescura + update incremental.
# =============================================================================
def _cache_dir() -> Path:
    """Directorio donde persisten los índices, FUERA del repo target.

    El índice es metadata DEL agente (no del proyecto sobre el que trabaja), así
    que no debe ensuciar el target con .agent_index.json — eso era la causa A del
    deadlock del git add (corrida #13): el diff arrastraba el index y el juez lo
    rechazaba como basura. Ahora vive en un cache separado, asociado al target por
    sha(root) (ver _cache_path): dos targets → dos archivos. Default
    ~/.cache/agent_juguete (fuera de cualquier repo del proyecto); override vía
    ZAI_INDEX_DIR. El cache NO cae dentro del target por defecto; si alguien
    apunta ZAI_INDEX_DIR dentro de un target, es decisión suya (los tests lo
    setean a un tmp separado del target para no auto-ensuciarse).
    """
    env = os.environ.get("ARNES_INDEX_DIR")
    if env:
        return Path(env)
    # Default: HERMES_HOME/arnes-cache (fuera del target, asociado al agente).
    hh = os.environ.get("HERMES_HOME")
    if hh:
        return Path(hh) / "arnes-cache"
    return Path.home() / ".cache" / "arnes-gates"


def _cache_path(root: str | Path) -> Path:
    """Path del archivo de índice para `root`: <cache_dir>/<sha(root resolved)>.json.

    sha sobre el root ABSOLUTO resuelto: dos rutas al mismo dir (relativa/absoluta/
    symlinks colapsados) → mismo archivo, y dos targets distintos → archivos
    distintos. Crea el cache_dir si no existe (mkdir parents).
    """
    digest = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()
    cache = _cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    return cache / f"{digest}.json"


def save_index(root: str | Path, index: dict[str, Any]) -> None:
    """Escribe el índice en el cache (fuera del target), no en <root>/.agent_index.json.

    Sobrescribe (estado derivado). load_index lo lee de vuelta vía _cache_path.
    """
    path = _cache_path(root)
    with path.open("w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, sort_keys=True, ensure_ascii=False)


def _es_cargable(index: dict[str, Any], root: str | Path) -> bool:
    """True si el índice cargado sirve para reconciliar: root y version calzan.

    Ya NO valida frescura por mtime (eso lo hace `reconcile` por archivo). Solo
    sanity: un índice de otro root o de otra versión del esquema no se reconcilia
    — se reconstruye desde cero (load_or_build → build_index).
    """
    return index.get("root") == str(Path(root).resolve()) and index.get("version") == INDEX_VERSION


def reconcile(index: dict[str, Any], root: str | Path) -> dict[str, Any]:
    """Aplica deltas al índice: solo reparsa los archivos que cambiaron.

    Por cada .py bajo root:
      - mtime igual al guardado → cache hit (no lee ni hashea; lo barato).
      - mtime cambió → lee + hashea; hash igual → touch sin cambio (actualiza
        mtime, no reparsa); hash distinto → reparsa (lo caro).
    Archivos nuevos → parsea; borrados → saca. Devuelve el mismo `index` mutado.
    Así el arranque amortiza el índice entre sesiones / tras un git pull, en vez
    de reconstruir todo cuando un .py cambió (que es lo que hacía el mtime global).
    """
    files = index["files"]
    en_disco = {str(p.relative_to(root)): p for p in _iter_py_files(root)}
    # Borrados: estaban indexados pero ya no están en disco.
    for rel in list(files):
        if rel not in en_disco:
            files.pop(rel)
    # Nuevos + cambiados.
    for rel, path in en_disco.items():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            files.pop(rel, None)
            continue
        entry = files.get(rel)
        if entry is not None and entry.get("mtime") == mtime:
            continue  # cache hit por mtime
        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            files.pop(rel, None)
            continue
        nuevo_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if entry is not None and entry.get("hash") == nuevo_hash:
            entry["mtime"] = mtime  # touch sin cambio de contenido: no reparsa
            continue
        nuevo = _parse_source(source, rel, root)
        if nuevo is None:  # syntax error: coherente con build_index/update_file
            files.pop(rel, None)
        else:
            nuevo["mtime"] = mtime
            files[rel] = nuevo
    return index


def load_index(root: str | Path) -> dict[str, Any] | None:
    """Lee el índice del cache si existe y es cargable; sino None.

    None = no existe, corrupto, o root/version no calzan. Ya NO filtra por mtime
    (lo hace `reconcile` por archivo): devuelve el índice crudo si es cargable.
    Así `load_or_build` puede reconciliar los deltas en vez de reconstruir todo.
    """
    path = _cache_path(root)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            index = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    return index if _es_cargable(index, root) else None


def load_or_build(root: str | Path) -> dict[str, Any]:
    """Carga y reconcilia el índice; si no hay (o no es cargable), lo construye."""
    index = load_index(root)
    return build_index(root) if index is None else reconcile(index, root)


def update_file(index: dict[str, Any], abs_path: str | Path, root: str | Path) -> dict[str, Any]:
    """Reparsa UN archivo y reemplaza su entrada en el índice (update incremental).

    Lo llama write_file tras escribir un .py, para que el índice no se desfasé
    durante la sesión sin reconstruir todo. Si el archivo no se puede parsear
    (syntax error, ilegible), se saca del índice — coherente con build_index.
    """
    path = Path(abs_path)
    entry = _parse_file(path, root)
    rel = str(path.relative_to(Path(root)))
    if entry is None:
        index["files"].pop(rel, None)
    else:
        index["files"][rel] = entry
    return index
