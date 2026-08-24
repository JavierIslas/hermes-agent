"""Tests del JavaAdapter (lang_java.py) — Nivel 1: regex + brace matching."""
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


def _la():
    from hermes_plugins.arnes_gates import lang_adapter
    return lang_adapter


JAVA_OK = """\
package com.x.ws;

import java.util.List;
import java.io.IOException;
import static java.util.Objects.requireNonNull;

public class Servicio {
    private int contador;

    public int getContador() {
        return contador;
    }

    public void procesar(List<String> items) throws IOException {
        for (String s : items) {
            System.out.println(s);
        }
    }
}
"""

JAVA_INCOMPLETO = "public class Roto {\n    int x = 1;\n"


# =============================================================================
# Registry.
# =============================================================================
def test_java_adapter_registrado():
    assert _la().get_adapter("java") is not None


def test_get_adapter_for_java():
    adapter = _la().get_adapter_for_path("Servicio.java")
    assert adapter is not None
    assert adapter.name == "java"


def test_extensions_java():
    assert ".java" in _la().all_extensions()


# =============================================================================
# parse_source.
# =============================================================================
def test_parse_source_package():
    adapter = _la().get_adapter("java")
    result = adapter.parse_source(JAVA_OK, "src/main/java/com/x/ws/Servicio.java")
    assert result is not None
    assert result["module"] == "com.x.ws.Servicio"


def test_parse_source_clase_y_metodos():
    adapter = _la().get_adapter("java")
    result = adapter.parse_source(JAVA_OK, "src/main/java/com/x/ws/Servicio.java")
    names = [s["name"] for s in result["symbols"]]
    assert "Servicio" in names
    assert "getContador" in names
    assert "procesar" in names


JAVA_TEST_LIKE = """\
package com.x;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;

class ServicioTest {
    @Test
    void incrementarSumaUno() {
        Servicio s = new Servicio();
        s.incrementar();
        assertEquals(2, s.getContador());
    }
}
"""


def test_parse_source_llamada_no_es_metodo():
    # Regresión (cazado en smoke real 2026-08-24): assertEquals(2, x) dentro
    # del cuerpo de un test se matcheaba como método fantasma. Una llamada
    # vive a profundidad de llaves >= 2; solo profundidad 1 es declaración.
    adapter = _la().get_adapter("java")
    result = adapter.parse_source(JAVA_TEST_LIKE, "ServicioTest.java")
    quals = [s["qualname"] for s in result["symbols"]]
    assert "ServicioTest.incrementarSumaUno" in quals  # void @Test: se indexa
    assert "ServicioTest.assertEquals" not in quals  # llamada: NO es método


def test_parse_source_qualname():
    adapter = _la().get_adapter("java")
    result = adapter.parse_source(JAVA_OK, "src/main/java/com/x/ws/Servicio.java")
    quals = [s["qualname"] for s in result["symbols"]]
    assert "Servicio.procesar" in quals
    assert "Servicio" in quals


def test_parse_source_signature():
    adapter = _la().get_adapter("java")
    result = adapter.parse_source(JAVA_OK, "src/main/java/com/x/ws/Servicio.java")
    by_name = {s["name"]: s for s in result["symbols"]}
    assert "List<String>" in by_name["procesar"]["signature"]
    assert "int" in by_name["getContador"]["signature"]


def test_parse_source_struct_hash_none():
    # Nivel 1: sin hash estructural — Q3/Q4 degradan honesto para Java.
    adapter = _la().get_adapter("java")
    result = adapter.parse_source(JAVA_OK, "src/main/java/com/x/ws/Servicio.java")
    assert all(s["struct_hash"] is None for s in result["symbols"])


def test_parse_source_imports():
    adapter = _la().get_adapter("java")
    result = adapter.parse_source(JAVA_OK, "src/main/java/com/x/ws/Servicio.java")
    imports = result["imports"]
    assert "java.util.List" in imports
    assert "java.io.IOException" in imports


def test_parse_source_imports_sin_paquete():
    # Archivo default-package: module derivado del nombre de archivo igual.
    adapter = _la().get_adapter("java")
    result = adapter.parse_source("public class Foo {}\n", "Foo.java")
    assert result is not None
    assert result["module"] == "Foo"


def test_parse_source_end_lineno_clase():
    # end_lineno correcto vía brace matching: la clase cierra en la línea 19
    # del fixture (package 1, imports 3-4, blank 5, clase 6..19).
    adapter = _la().get_adapter("java")
    result = adapter.parse_source(JAVA_OK, "Servicio.java")
    by_name = {s["name"]: s for s in result["symbols"]}
    assert by_name["Servicio"]["end_lineno"] == 19
    assert by_name["procesar"]["end_lineno"] == 18  # cierra antes que la clase


# =============================================================================
# check_syntax: balance de llaves (débil a propósito — javac es el gate real).
# =============================================================================
def test_check_syntax_ok():
    adapter = _la().get_adapter("java")
    assert adapter.check_syntax(JAVA_OK, "Servicio.java") is None


def test_check_syntax_llaves_desbalanceadas():
    adapter = _la().get_adapter("java")
    err = adapter.check_syntax(JAVA_INCOMPLETO, "Roto.java")
    assert err is not None
    assert "llaves" in err.lower() or "braces" in err.lower()


# =============================================================================
# extract_usages.
# =============================================================================
def test_extract_usages():
    adapter = _la().get_adapter("java")
    usages = adapter.extract_usages("int y = servicio.procesar(items);")
    assert "servicio" in usages
    assert "procesar" in usages
    assert "items" in usages


def test_extract_usages_vacio():
    adapter = _la().get_adapter("java")
    assert adapter.extract_usages("") == set()


# =============================================================================
# DIRS_RUIDO: target/ (maven), build/ (gradle), out/, .gradle/, .idea/ no se
# indexan — sin esto el índice comería bytecode generado.
# =============================================================================
def test_dirs_ruido_java():
    from hermes_plugins.arnes_gates.index import DIRS_RUIDO

    assert "target" in DIRS_RUIDO
    assert "build" in DIRS_RUIDO
    assert "out" in DIRS_RUIDO
    assert ".gradle" in DIRS_RUIDO
    assert ".idea" in DIRS_RUIDO


def test_iter_source_files_salta_target(tmp_path):
    from hermes_plugins.arnes_gates.index import _iter_source_files

    (tmp_path / "src" / "Main.java").parent.mkdir(parents=True)
    (tmp_path / "src" / "Main.java").write_text("public class Main {}\n", encoding="utf-8")
    (tmp_path / "target" / "classes" / "Main.class").parent.mkdir(parents=True)
    (tmp_path / "target" / "classes" / "Main.class").write_text("binary", encoding="utf-8")
    (tmp_path / "target" / "classes" / "Fake.java").write_text(
        "// generado\npublic class Fake {}\n", encoding="utf-8"
    )

    files = list(_iter_source_files(tmp_path))
    rels = [str(f.relative_to(tmp_path)) for f in files]
    assert "src/Main.java" in rels
    assert all("target" not in r for r in rels)
