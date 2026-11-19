"""A mutation run, and the test suite itself, leave no temporary files behind.

`scripts/mutate.py` runs the whole suite once per rule, and every run made its
own pytest folder of temporary files. Over a day of runs those folders filled
the machine's temporary space until no program could start a command. Each run
now gets a folder of its own, outside the checkout, removed when the run ends;
whatever a killed run left is swept away at the next start.
"""

import contextlib
import importlib.util
import os
import pathlib
import signal
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).parent.parent

LEAVES_FILES = """
import os

def test_it_writes_files_it_cannot_delete(tmp_path):
    (tmp_path / "plain.txt").write_text("x")
    (tmp_path / "locked.txt").write_text("x")
    os.chmod(tmp_path / "locked.txt", 0)
    (tmp_path / "shut").mkdir()
    (tmp_path / "shut" / "inside.txt").write_text("x")
    os.chmod(tmp_path / "shut", 0)
"""


#: The bound on a test's own scratch run of the suite: the template's one number, generous
#: enough that it fires only on a real hang.
WITHIN = 300


def _mutate(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("mutate_temp", ROOT / "scripts/mutate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_leaves_files.py").write_text(LEAVES_FILES, encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", project)
    monkeypatch.setattr(module, "MUTATE_TMP", tmp_path / "mutate-tmp")
    return module


def test_a_suite_run_leaves_no_temporary_files_behind(tmp_path, monkeypatch):
    mutate = _mutate(tmp_path, monkeypatch)
    failed, _ = mutate.run_suite(timeout=WITHIN)
    assert failed == 0
    assert not mutate.MUTATE_TMP.exists()


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows needs extra rights to make a link, and has no 0755"
)
def test_removing_a_run_s_files_never_changes_a_folder_it_links_to(tmp_path, monkeypatch):
    mutate = _mutate(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    (outside / "kept.txt").write_text("x")
    (mutate.ROOT / "tests" / "test_links_out.py").write_text(
        f"def test_it_links_out(tmp_path):\n    (tmp_path / 'link').symlink_to({str(outside)!r})\n"
    )
    failed, _ = mutate.run_suite(timeout=WITHIN)
    assert failed == 0
    assert (outside.stat().st_mode & 0o777, (outside / "kept.txt").exists()) == (0o755, True)


def test_a_killed_run_s_temporary_files_are_swept_at_the_next_start(tmp_path, monkeypatch):
    mutate = _mutate(tmp_path, monkeypatch)
    left = mutate.MUTATE_TMP / "run-left-by-a-killed-run"
    (left / "shut").mkdir(parents=True)
    (left / "shut" / "inside.txt").write_text("x")
    (left / ".lock").touch()  # a run locks it as it starts; a killed run's lock is free
    (left / "shut").chmod(0)
    mutate.MUTATE_TMP.chmod(0o700)  # as a real run makes it, for its user alone
    mutate.sweep()
    assert not left.exists()


def test_the_temporary_files_live_outside_the_checkout():
    """Inside it, a test that makes a git repository of its own would find this one instead."""
    spec = importlib.util.spec_from_file_location("mutate_where", ROOT / "scripts/mutate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert ROOT.resolve() not in module.MUTATE_TMP.resolve().parents


def test_a_passing_test_keeps_no_temporary_files():
    settings = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'tmp_path_retention_policy = "failed"' in settings
    assert "tmp_path_retention_count = 1" in settings


#: A test that starts a program sharing its output, notes its pid, then hangs as long.
STARTS_AND_HANGS = """
import pathlib, subprocess, sys, time

def test_it_starts_a_program_and_hangs():
    started = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    pathlib.Path({pid_file!r}).write_text(str(started.pid))
    time.sleep(30)
"""


def _alive(pid):
    """Whether `pid` is still the program the test started, not ended, and not some later reuse."""
    try:
        cmdline = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
        state = pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return b"time.sleep(30)" in cmdline and state != "Z"


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc to see the program has ended")
def test_a_scratch_run_that_overruns_leaves_nothing_it_started_running(tmp_path, monkeypatch):
    mutate = _mutate(tmp_path, monkeypatch)
    pid_file = tmp_path / "started.pid"
    (mutate.ROOT / "tests" / "test_hangs.py").write_text(
        STARTS_AND_HANGS.format(pid_file=str(pid_file)), encoding="utf-8"
    )
    began = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            mutate.run_suite(timeout=5)
        took = time.monotonic() - began
        pid = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 5
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(pid), "the program the scratch run's test started is still running"
        assert took < 20, f"the overrun took {took:.0f} s to end, waiting on what its test started"
    finally:
        with contextlib.suppress(FileNotFoundError, ValueError):
            pid = int(pid_file.read_text(encoding="utf-8"))
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
