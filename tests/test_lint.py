"""Catch undefined names before a run does.

Moving a function between modules left `unreviewed_pages` referenced but not
defined, and nothing caught it until the coordinator reached that line twelve
minutes into a paid run. `py_compile` cannot see it -- a NameError is a runtime
error -- so this test runs pyflakes over the package and fails on any undefined
name. It skips when pyflakes is not installed, so it never blocks a user.
"""
from pathlib import Path
import subprocess
import sys

import pytest

PKG = Path(__file__).resolve().parents[1] / "paperskill"
ROOT = Path(__file__).resolve().parents[1]


def _pyflakes_available() -> bool:
    try:
        import pyflakes  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _pyflakes_available(), reason="pyflakes not installed")
def test_no_undefined_names_in_package():
    proc = subprocess.run([sys.executable, "-m", "pyflakes", str(PKG), str(ROOT / "run.py")],
                          capture_output=True, text=True)
    bad = [ln for ln in proc.stdout.splitlines()
           if "undefined name" in ln or "undefined local" in ln]
    assert not bad, "undefined names:\n" + "\n".join(bad)


@pytest.mark.skipif(not _pyflakes_available(), reason="pyflakes not installed")
def test_no_unused_imports_or_stale_references():
    """Not fatal, but a stale import is usually the fossil of a moved function."""
    proc = subprocess.run([sys.executable, "-m", "pyflakes", str(PKG), str(ROOT / "run.py")],
                          capture_output=True, text=True)
    bad = [ln for ln in proc.stdout.splitlines() if "imported but unused" in ln]
    assert not bad, "stale imports:\n" + "\n".join(bad)


def test_every_coordinator_name_resolves():
    """Backstop that needs no third-party tool: import the agent modules and check
    that every module-level name the coordinator calls actually exists."""
    pytest.importorskip("agents")
    import ast
    import paperskill.coordinator as co
    src = (PKG / "coordinator.py").read_text()
    tree = ast.parse(src)
    defined = set(dir(co)) | set(dir(__builtins__ if isinstance(__builtins__, dict)
                                   else __builtins__))
    if isinstance(__builtins__, dict):
        defined |= set(__builtins__)
    local: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local.add(node.name)
            local |= {a.arg for a in node.args.args + node.args.kwonlyargs}
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            local.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                local.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.comprehension):
            for t in ast.walk(node.target):
                if isinstance(t, ast.Name):
                    local.add(t.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            local.add(node.name)
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    missing = sorted(called - defined - local)
    assert not missing, f"coordinator calls names that do not resolve: {missing}"
