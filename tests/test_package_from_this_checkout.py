"""The checks measure this checkout's own code, never another worktree's.

Two worktrees that share one installed environment import whichever of them
installed the plugin last. Every rule would then be measured against the other
one's code, and all of them would look alike. Both scripts check this before
they measure, in a fresh interpreter set up as the measuring steps will be.
"""

import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_package", ROOT / f"scripts/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _two_checkouts(tmp_path):
    """Two checkouts, each holding a package of the same name."""
    here, other = tmp_path / "here", tmp_path / "other"
    for root in (here, other):
        (root / "src/pkg").mkdir(parents=True)
        (root / "src/pkg/__init__.py").write_text("", encoding="utf-8")
    return here, other


def test_a_mutation_run_refuses_a_package_from_another_checkout(tmp_path, monkeypatch):
    """A venv shared with another worktree measures that worktree's code, and every rule looks the same."""
    mutate = _load("mutate")
    here, other = _two_checkouts(tmp_path)
    monkeypatch.setattr(mutate, "ROOT", here)
    monkeypatch.setenv("PYTHONPATH", str(other / "src"))
    with pytest.raises(SystemExit, match="uv sync") as refused:
        mutate.check_the_package()
    assert str(other / "src/pkg") in str(refused.value)
    assert str(here / "src") in str(refused.value)


def test_a_mutation_run_measures_a_package_from_its_own_checkout(tmp_path, monkeypatch):
    mutate = _load("mutate")
    here, _ = _two_checkouts(tmp_path)
    monkeypatch.setattr(mutate, "ROOT", here)
    monkeypatch.setenv("PYTHONPATH", str(here / "src"))
    mutate.check_the_package()


def test_a_mutation_run_checks_the_package_before_it_measures(tmp_path, monkeypatch):
    mutate = _load("mutate")
    monkeypatch.setattr(mutate, "ROOT", tmp_path)
    monkeypatch.setattr(mutate, "ROWS", tmp_path / "scripts/mutations")
    monkeypatch.setattr(mutate, "RECOVERY", tmp_path / "recovery.json")
    monkeypatch.setattr(mutate, "MUTATE_TMP", tmp_path / "kassalapp-mcp-mutate-0000aaaa")
    monkeypatch.setattr(mutate.sys, "argv", ["mutate.py"])

    def refused():
        msg = "from another checkout"
        raise SystemExit(msg)

    monkeypatch.setattr(mutate, "check_the_package", refused)
    with pytest.raises(SystemExit, match="another checkout"):
        mutate.main()
