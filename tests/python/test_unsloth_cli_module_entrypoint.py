import runpy
import sys
import types
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_MAIN = REPO_ROOT / "unsloth_cli" / "__main__.py"
CLI_INIT = REPO_ROOT / "unsloth_cli" / "__init__.py"
INSTALL_PS1 = REPO_ROOT / "install.ps1"


def test_python_module_entrypoint_invokes_the_cli(monkeypatch):
    calls = []
    package = types.ModuleType("unsloth_cli")
    package.app = lambda: calls.append("called")
    monkeypatch.setitem(sys.modules, "unsloth_cli", package)

    runpy.run_path(str(CLI_MAIN), run_name = "__main__")

    assert calls == ["called"]


def test_module_entrypoint_gets_console_script_guards():
    source = CLI_INIT.read_text(encoding = "utf-8")
    assert '"__main__.py"' in source


def test_windows_studio_setup_uses_the_module_entrypoint():
    source = INSTALL_PS1.read_text(encoding = "utf-8")
    assert "& $VenvPython -m unsloth_cli @studioArgs" in source
    assert "& $UnslothExe @studioArgs" not in source
