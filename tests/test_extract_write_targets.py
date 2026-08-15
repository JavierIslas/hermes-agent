"""Tests de extract_write_targets: extraer paths de destino de comandos shell."""
import sys
import types
import importlib.util
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "arnes-gates"


@pytest.fixture(scope="module", autouse=True)
def _tg():
    """Carga el modulo terminal_guard del plugin standalone."""
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
    from hermes_plugins.arnes_gates.terminal_guard import extract_write_targets
    return extract_write_targets


def _tgt():
    """Acceso lazy al modulo cargado."""
    from hermes_plugins.arnes_gates.terminal_guard import extract_write_targets
    return extract_write_targets


# =============================================================================
# Redireccion simple y append.
# =============================================================================
def test_redirect_simple():
    assert _tgt()("echo x > foo.py") == ["foo.py"]


def test_redirect_append():
    assert _tgt()("echo x >> foo.py") == ["foo.py"]


def test_redirect_glued():
    assert _tgt()("echo x >foo.py") == ["foo.py"]


def test_redirect_append_glued():
    assert _tgt()("echo x >>foo.py") == ["foo.py"]


def test_redirect_with_path():
    assert _tgt()("echo x > /tmp/test.py") == ["/tmp/test.py"]


def test_redirect_quoted_path():
    assert _tgt()('echo x > "my file.py"') == ["my file.py"]


# =============================================================================
# Heredoc.
# =============================================================================
def test_heredoc_redirect():
    assert _tgt()("cat <<EOF > foo.py") == ["foo.py"]


def test_heredoc_append():
    assert _tgt()("cat <<EOF >> foo.py") == ["foo.py"]


# =============================================================================
# cp / mv / install.
# =============================================================================
def test_cp_simple():
    assert _tgt()("cp src.py dst.py") == ["dst.py"]


def test_cp_with_flags():
    assert _tgt()("cp -r src/ dst/") == ["dst/"]


def test_cp_force():
    assert _tgt()("cp -f src.py dst.py") == ["dst.py"]


def test_mv_simple():
    assert _tgt()("mv old.py new.py") == ["new.py"]


def test_install_simple():
    result = _tgt()("install -m 755 script.sh /usr/bin/script")
    assert "/usr/bin/script" in result


# =============================================================================
# sed -i.
# =============================================================================
def test_sed_inplace():
    assert _tgt()("sed -i 's/old/new/' file.py") == ["file.py"]


def test_sed_inplace_multiple_files():
    result = _tgt()("sed -i 's/old/new/' a.py b.py")
    assert "a.py" in result
    assert "b.py" in result


def test_sed_no_inplace():
    """sed sin -i no escribe — no debe extraer targets."""
    assert _tgt()("sed 's/old/new/' file.py") == []


# =============================================================================
# dd.
# =============================================================================
def test_dd_of():
    assert _tgt()("dd if=/dev/zero of=file.bin bs=1k count=1") == ["file.bin"]


# =============================================================================
# tee.
# =============================================================================
def test_tee_single():
    result = _tgt()("echo x | tee foo.py")
    assert result == ["foo.py"]


def test_tee_multiple():
    result = _tgt()("echo x | tee a.py b.py")
    assert "a.py" in result
    assert "b.py" in result


def test_tee_append():
    result = _tgt()("echo x | tee -a foo.py")
    assert result == ["foo.py"]


# =============================================================================
# Casos negativos — no deben extraer nada.
# =============================================================================
def test_no_write_ls():
    assert _tgt()("ls -la") == []


def test_no_write_grep():
    assert _tgt()("grep -r 'pattern' .") == []


def test_no_write_git():
    assert _tgt()("git status") == []


def test_no_write_cat_stdout():
    """cat sin redireccion va a stdout — no escribe archivo."""
    assert _tgt()("cat file.py") == []


def test_no_write_echo_stdout():
    assert _tgt()("echo hello world") == []


def test_empty_command():
    assert _tgt()("") == []


# =============================================================================
# Dedup.
# =============================================================================
def test_dedup_same_target():
    """echo x > foo.py → foo.py una sola vez."""
    result = _tgt()("echo x > foo.py")
    assert result.count("foo.py") == 1
