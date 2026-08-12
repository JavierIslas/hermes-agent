"""Tests del detector de write-via-terminal."""
import sys
import types
import importlib.util
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent / "arnes-gates"


@pytest.fixture(autouse=True)
def _load_terminal_guard(monkeypatch):
    """Carga solo terminal_guard.py (no el plugin entero)."""
    for mod in list(sys.modules):
        if "arnes" in mod or "hermes_plugins" in mod:
            del sys.modules[mod]

    ns = types.ModuleType("hermes_plugins")
    ns.__path__ = []
    ns.__package__ = "hermes_plugins"
    sys.modules["hermes_plugins"] = ns

    module_name = "hermes_plugins.arnes_gates.terminal_guard"
    spec = importlib.util.spec_from_file_location(
        module_name,
        str(PLUGIN_DIR / "terminal_guard.py"),
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "hermes_plugins.arnes_gates"
    module.__path__ = [str(PLUGIN_DIR)]
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    yield


def _detect(cmd):
    from hermes_plugins.arnes_gates.terminal_guard import detect_write_command
    return detect_write_command(cmd)


# =============================================================================
# Tests: deteccion de patrones HIGH confidence
# =============================================================================
def test_detect_heredoc():
    d = _detect("cat <<EOF > foo.py")
    assert d.is_write is True
    assert d.confidence == "high"


def test_detect_heredoc_plain():
    d = _detect("python << 'PYEOF'")
    assert d.is_write is True
    assert d.confidence == "high"


def test_detect_redirection():
    d = _detect("echo x > foo.py")
    assert d.is_write is True
    assert d.confidence == "high"


def test_detect_append():
    d = _detect("echo x >> foo.py")
    assert d.is_write is True
    assert d.confidence == "high"


def test_detect_sed_inplace():
    d = _detect("sed -i 's/a/b/' foo.py")
    assert d.is_write is True
    assert d.confidence == "high"


def test_detect_dd_of():
    d = _detect("dd if=/dev/zero of=file.bin bs=1k count=1")
    assert d.is_write is True
    assert d.confidence == "high"


# =============================================================================
# Tests: deteccion de patrones MEDIUM confidence
# =============================================================================
def test_detect_tee():
    d = _detect("echo x | tee foo.py")
    assert d.is_write is True
    assert d.confidence == "medium"


def test_detect_cp():
    d = _detect("cp src.py dst.py")
    assert d.is_write is True
    assert d.confidence == "medium"


def test_detect_mv():
    d = _detect("mv old.py new.py")
    assert d.is_write is True
    assert d.confidence == "medium"


# =============================================================================
# Tests: comandos que NO escriben (no deben bloquear)
# =============================================================================
def test_no_write_readonly_cat():
    d = _detect("cat foo.py | grep bar")
    assert d.is_write is False


def test_no_write_ls():
    d = _detect("ls -la")
    assert d.is_write is False


def test_no_write_grep():
    d = _detect("grep -r 'pattern' .")
    assert d.is_write is False


def test_no_write_python_run():
    d = _detect("python -m pytest tests/")
    assert d.is_write is False


def test_no_write_git_status():
    d = _detect("git status")
    assert d.is_write is False


def test_no_write_git_diff():
    d = _detect("git diff HEAD")
    assert d.is_write is False


def test_no_write_pip_install():
    """pip install escribe al venv pero NO al repo — no debe bloquear."""
    d = _detect("pip install requests")
    assert d.is_write is False


# =============================================================================
# Tests: should_block (politica)
# =============================================================================
def test_should_block_high():
    from hermes_plugins.arnes_gates.terminal_guard import should_block, WriteDetection
    assert should_block(WriteDetection(True, "high", "test")) is True


def test_should_block_medium():
    from hermes_plugins.arnes_gates.terminal_guard import should_block, WriteDetection
    assert should_block(WriteDetection(True, "medium", "test")) is True


def test_should_not_block_low():
    from hermes_plugins.arnes_gates.terminal_guard import should_block, WriteDetection
    assert should_block(WriteDetection(True, "low", "editor")) is False


def test_should_not_block_no_write():
    from hermes_plugins.arnes_gates.terminal_guard import should_block, WriteDetection
    assert should_block(WriteDetection(False, "high", "readonly")) is False


# =============================================================================
# Tests: edge cases
# =============================================================================
def test_empty_command():
    d = _detect("")
    assert d.is_write is False


def test_redirection_in_string_not_detected():
    """Un > dentro de un string NO debe contar como redireccion."""
    d = _detect('echo "use > for redirect"')
    # El echo mismo no redirige, pero el string tiene >. shlex lo separa bien.
    # Si el > esta dentro del string, no es un token separado.
    assert d.is_write is False


def test_multiple_redirects():
    d = _detect("echo x > /tmp/a > /tmp/b")
    assert d.is_write is True
    assert d.confidence == "high"
