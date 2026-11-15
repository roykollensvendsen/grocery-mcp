"""A mutation run, and the quality check, leave nothing behind, even when stopped.

Both scripts make temporary folders: the mutation run one per run of the suite,
the quality check a whole copy of main with its packages installed. Python
removes them when a script ends, or is stopped with Ctrl-C. A plain ``kill``
skipped that, and every checkout and every copy has a folder named after its
own path, so nothing came back to sweep it. Hundreds of them filled ``/tmp``.
"""

import importlib.util
import os
import pathlib
import signal
import subprocess
import sys
import textwrap
import time

import pytest

ROOT = pathlib.Path(__file__).parent.parent

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Windows has no SIGTERM to send")


def _project(tmp_path, test_body):
    """A tiny checkout with one rule, its row, and the test named after it."""
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src/code.py").write_text("# RULE: x is one\nx = 1\n", encoding="utf-8")
    (project / "tests").mkdir()
    (project / "tests/test_code.py").write_text(test_body, encoding="utf-8")
    rows = project / "scripts/mutations/src/code.py"
    rows.mkdir(parents=True)
    (rows / "x-is-one.toml").write_text(
        "[[rule]]\nname = \"x is one\"\nfile = \"src/code.py\"\nfind = '^x = 1$'\nreplace = 'x = 2'\n",
        encoding="utf-8",
    )
    return project


CHECKS_X = (
    'import pathlib\n\ndef test_x_is_one():\n    assert "x = 1" in pathlib.Path("src/code.py").read_text()\n'
)
HANGS = "import time\n\ndef test_x_is_one():\n    time.sleep(60)\n"


def _runner(project, tmp_path, then=""):
    """A script that runs the real mutation check on `project`, with its folders under `tmp_path`.

    `then` is set-up run after the paths are set, as lines of Python using `mutate`.
    """
    script = tmp_path / "run_mutate.py"
    script.write_text(
        textwrap.dedent(f"""
            import importlib.util, pathlib, sys
            spec = importlib.util.spec_from_file_location("mutate", {str(ROOT / "scripts/mutate.py")!r})
            mutate = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mutate)
            mutate.ROOT = pathlib.Path({str(project)!r})
            mutate.ROWS = mutate.ROOT / "scripts/mutations"
            mutate.RECOVERY = pathlib.Path({str(tmp_path / "recovery.json")!r})
            mutate.MUTATE_TMP = pathlib.Path({str(tmp_path / "mutate-tmp")!r})
            sys.argv = ["mutate.py"]
        """)
        + textwrap.dedent(then)
        + "sys.exit(mutate.main())\n",
        encoding="utf-8",
    )
    return script


def test_a_mutation_run_leaves_no_folder_of_its_own_behind(tmp_path):
    project = _project(tmp_path, CHECKS_X)
    done = subprocess.run(
        [sys.executable, str(_runner(project, tmp_path))], capture_output=True, text=True, check=False
    )
    assert "1/1 rules turned off" in done.stdout, done.stdout + done.stderr
    assert not (tmp_path / "mutate-tmp").exists()


@pytest.mark.parametrize(
    "stop", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP], ids=["a kill", "Ctrl-C", "a closed terminal"]
)
def test_a_stopped_mutation_run_still_puts_everything_back(tmp_path, stop):
    """Stopped while its suite runs, the run puts the file back and removes its folder.

    GitHub cancels a job with SIGINT and then SIGTERM; a closed terminal sends SIGHUP.
    """
    project = _project(tmp_path, HANGS)
    running = subprocess.Popen(
        [sys.executable, str(_runner(project, tmp_path))],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    source = project / "src/code.py"
    deadline = time.monotonic() + 30
    while "x = 2" not in source.read_text(encoding="utf-8"):
        assert time.monotonic() < deadline, "the run never switched the rule off"
        time.sleep(0.05)
    time.sleep(1)  # let its suite start
    running.send_signal(stop)
    running.wait(timeout=30)
    assert source.read_text(encoding="utf-8") == "# RULE: x is one\nx = 1\n"
    assert not (tmp_path / "mutate-tmp").exists()
    assert not (tmp_path / "recovery.json").exists()


def _ratchet():
    spec = importlib.util.spec_from_file_location("ratchet_stop", ROOT / "scripts/ratchet.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_stopped_quality_check_leaves_no_copy_behind(tmp_path):
    """The check stops as Ctrl-C would stop it, so its temporary copy of main is removed."""
    holder = tmp_path / "holder.py"
    holder.write_text(
        textwrap.dedent(f"""
            import importlib.util, sys, tempfile, time
            spec = importlib.util.spec_from_file_location("ratchet", {str(ROOT / "scripts/ratchet.py")!r})
            ratchet = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(ratchet)
            ratchet.stop_cleanly_when_killed()
            with tempfile.TemporaryDirectory(dir={str(tmp_path)!r}, prefix="copy-") as copy:
                print(copy, flush=True)
                time.sleep(60)
        """),
        encoding="utf-8",
    )
    running = subprocess.Popen([sys.executable, str(holder)], stdout=subprocess.PIPE, text=True)
    copy = pathlib.Path(running.stdout.readline().strip())
    assert copy.is_dir()
    running.send_signal(signal.SIGTERM)
    running.wait(timeout=30)
    assert not copy.exists()
    assert [p.name for p in tmp_path.iterdir()] == ["holder.py"]


def _first_stop_leaves_the_rest_ignored(module):
    """Install `module`'s stop handling, stop once, and say which stops are then ignored."""
    stops = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    before = {each: signal.getsignal(each) for each in stops}
    try:
        module.stop_cleanly_when_killed()
        with pytest.raises(SystemExit):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        return {each: signal.getsignal(each) == signal.SIG_IGN for each in stops}
    finally:
        for each, handler in before.items():
            signal.signal(each, handler)


def _mutate():
    spec = importlib.util.spec_from_file_location("mutate_stop", ROOT / "scripts/mutate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_after_the_first_stop_the_mutation_run_ignores_the_rest():
    assert all(_first_stop_leaves_the_rest_ignored(_mutate()).values())


#: Slows the clean-up after a stop, and says when it has begun, so a second stop lands in it.
SLOW_CLEANUP = """
import time
real_remove = mutate._remove

def slow_remove(folder):
    pathlib.Path({began!r}).touch()
    time.sleep(2)
    real_remove(folder)

mutate._remove = slow_remove
"""


def _waits_for(condition, what):
    deadline = time.monotonic() + 30
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.05)


def test_a_second_stop_does_not_cut_the_cleanup_short(tmp_path):
    """GitHub cancels a job with SIGINT, then SIGTERM a moment later, while the clean-up runs."""
    project = _project(tmp_path, HANGS)
    began = tmp_path / "cleanup-began"
    script = _runner(project, tmp_path, SLOW_CLEANUP.format(began=str(began)))
    running = subprocess.Popen([sys.executable, str(script)], text=True)
    source = project / "src/code.py"
    _waits_for(lambda: "x = 2" in source.read_text(encoding="utf-8"), "the run never switched the rule off")
    time.sleep(1)  # let its suite start
    running.send_signal(signal.SIGINT)
    _waits_for(began.exists, "the clean-up never began")
    running.send_signal(signal.SIGTERM)
    running.wait(timeout=60)
    assert source.read_text(encoding="utf-8") == "# RULE: x is one\nx = 1\n"
    assert not (tmp_path / "mutate-tmp").exists(), "the second stop cut the clean-up short"


def test_a_second_stop_does_not_cut_the_quality_check_s_cleanup_short():
    assert all(_first_stop_leaves_the_rest_ignored(_ratchet()).values())


def _run_folder(parent, name="run-left-by-a-killed-run", *, locked=True):
    """A run's folder as a killed run leaves it: its lock file, free, and its files."""
    folder = parent / name
    (folder / "basetemp").mkdir(parents=True)
    (folder / "basetemp" / "inside.txt").write_text("x", encoding="utf-8")
    if locked:
        (folder / ".lock").touch()
    parent.chmod(0o700)  # as a real run makes it, for its user alone
    return folder


def test_a_dead_run_s_folder_is_swept_in_any_checkout(tmp_path, monkeypatch):
    """A copy the quality check made is never run in again, so another checkout sweeps what it left."""
    mutate = _mutate()
    monkeypatch.setattr(mutate, "MUTATE_TMP", tmp_path / "kassalapp-mcp-mutate-0000aaaa")
    other = tmp_path / "kassalapp-mcp-mutate-1111bbbb"
    _run_folder(other)
    not_ours = _run_folder(tmp_path / "someone-else-mutate-2222cccc")
    mutate.sweep()
    assert not other.exists()
    assert not_ours.exists()


def test_a_run_still_going_is_never_swept(tmp_path, monkeypatch):
    """Two runs at once, in one checkout or two: the one starting must not clear the other's folder."""
    import fcntl  # noqa: PLC0415

    mutate = _mutate()
    monkeypatch.setattr(mutate, "MUTATE_TMP", tmp_path / "kassalapp-mcp-mutate-0000aaaa")
    going = _run_folder(mutate.MUTATE_TMP, "run-still-going")
    held = os.open(going / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(held, fcntl.LOCK_EX)
        mutate.sweep()
        assert (going / "basetemp" / "inside.txt").exists()
    finally:
        os.close(held)


def test_a_new_run_s_folder_is_locked_before_it_takes_a_run_s_name(tmp_path, monkeypatch):
    """A sweep in between would see a run's name with no lock held, and remove it."""
    import fcntl  # noqa: PLC0415

    mutate = _mutate()
    monkeypatch.setattr(mutate, "MUTATE_TMP", tmp_path / "kassalapp-mcp-mutate-0000aaaa")
    named_when_locked = []
    real_flock = fcntl.flock

    def flock(fd, how):
        named_when_locked.append(sorted(p.name for p in mutate.MUTATE_TMP.glob("run-*")))
        return real_flock(fd, how)

    monkeypatch.setattr(mutate.fcntl, "flock", flock)
    folder, lock = mutate.start_run_folder()
    try:
        assert named_when_locked == [[]]
        assert folder.name.startswith("run-")
    finally:
        os.close(lock)


def test_pytest_s_own_clearing_never_takes_a_running_run_s_lock(tmp_path):
    """When it starts, pytest empties the folder given as --basetemp, so the lock must sit beside it."""
    project = _project(tmp_path, HANGS)
    running = subprocess.Popen([sys.executable, str(_runner(project, tmp_path))], text=True)
    source = project / "src/code.py"
    deadline = time.monotonic() + 30
    while "x = 2" not in source.read_text(encoding="utf-8"):
        assert time.monotonic() < deadline, "the run never switched the rule off"
        time.sleep(0.05)
    time.sleep(2)  # its suite has started, and pytest has cleared its basetemp
    try:
        mutate = _mutate()
        mutate.MUTATE_TMP = tmp_path / "mutate-tmp"
        mutate.sweep()
        assert list((tmp_path / "mutate-tmp").glob("run-*")), "a sweep removed a run still going"
    finally:
        running.send_signal(signal.SIGTERM)
        running.wait(timeout=30)


CHANGES_A_STOP = """
import signal

def test_one_installs_stop_handlers():
    for each in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(each, signal.SIG_IGN)

def test_two_finds_them_as_they_were():
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
    assert signal.getsignal(signal.SIGHUP) is signal.SIG_DFL
"""


def test_one_test_s_stop_handlers_never_reach_the_next(tmp_path):
    """A test calling a script's main() installs its stop handlers in the test run itself.

    Left there, a kill would no longer end the test run, and after one stop the rest
    would be ignored. The conftest puts the three signals back after every test.
    """
    (tmp_path / "tests").mkdir()
    conftest = (ROOT / "tests/conftest.py").read_text(encoding="utf-8")
    (tmp_path / "tests/conftest.py").write_text(conftest, encoding="utf-8")
    (tmp_path / "tests/test_stops.py").write_text(CHANGES_A_STOP, encoding="utf-8")
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(tmp_path), "tests"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout


def test_a_claimed_folder_with_no_lock_yet_is_never_swept(tmp_path, monkeypatch):
    """A new run claims its name, then moves its locked folder in; a sweep between must leave it."""
    mutate = _mutate()
    monkeypatch.setattr(mutate, "MUTATE_TMP", tmp_path / "kassalapp-mcp-mutate-0000aaaa")
    claimed = mutate.MUTATE_TMP / "run-000000000001"
    claimed.mkdir(parents=True)
    mutate.MUTATE_TMP.chmod(0o700)
    mutate.sweep()
    assert claimed.is_dir()
    assert not (claimed / mutate.LOCK).exists(), "a sweep made a lock in another run's folder"


def test_an_hour_old_folder_with_no_lock_is_swept(tmp_path, monkeypatch):
    """Left by a run killed in that instant, or by a run from before runs held locks."""
    mutate = _mutate()
    monkeypatch.setattr(mutate, "MUTATE_TMP", tmp_path / "kassalapp-mcp-mutate-0000aaaa")
    left = _run_folder(mutate.MUTATE_TMP, "run-000000000002", locked=False)
    old = time.time() - mutate.STARTING_SECONDS - 60
    os.utime(left, (old, old))
    mutate.sweep()
    assert not left.exists()


def test_a_folder_another_sweep_removed_first_is_passed_over(tmp_path, monkeypatch):
    """Two runs may sweep at once; the one that comes second finds the folder already gone."""
    mutate = _mutate()
    monkeypatch.setattr(mutate, "MUTATE_TMP", tmp_path / "kassalapp-mcp-mutate-0000aaaa")
    gone = mutate.MUTATE_TMP / "run-000000000003"
    gone.mkdir(parents=True)
    mutate.MUTATE_TMP.chmod(0o700)
    real_open = mutate.os.open

    def open_after_the_other_sweep(path, *args, **kwargs):
        if gone.exists():
            gone.rmdir()  # the other sweep removes it after this one first looked
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(mutate.os, "open", open_after_the_other_sweep)
    mutate.sweep()  # passes over it, rather than fail the run
    assert not gone.exists()


@pytest.mark.parametrize("planted", ["open to others", "a link", "a file"])
def test_a_run_trusts_its_folder_only_when_it_is_really_ours(tmp_path, monkeypatch, planted):
    """The folder's name is easy to guess in a /tmp everyone shares; what is planted there is refused."""
    mutate = _mutate()
    place = tmp_path / "kassalapp-mcp-mutate-0000aaaa"
    monkeypatch.setattr(mutate, "MUTATE_TMP", place)
    if planted == "open to others":
        place.mkdir(mode=0o700)
        place.chmod(0o755)
    elif planted == "a link":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        place.symlink_to(elsewhere)
    else:
        place.write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit, match=str(place)) as refused:
        mutate.start_run_folder()
    fix = f"chmod 700 {place}" if planted == "open to others" else f"rm -r -- {place}"
    assert fix in str(refused.value), "the refusal gives the command that fixes it"
    assert not any(p.name.startswith(("run-", "starting-")) for p in tmp_path.rglob("*"))


def test_a_run_s_own_folder_is_made_for_its_user_alone(tmp_path, monkeypatch):
    mutate = _mutate()
    monkeypatch.setattr(mutate, "MUTATE_TMP", tmp_path / "kassalapp-mcp-mutate-0000aaaa")
    folder, lock = mutate.start_run_folder()
    try:
        assert mutate.MUTATE_TMP.stat().st_mode & 0o777 == 0o700
        assert folder.parent == mutate.MUTATE_TMP
    finally:
        os.close(lock)


def test_a_new_run_s_lock_is_always_one_it_made(tmp_path, monkeypatch):
    """A lock file already in place is never taken as a new run's own."""
    mutate = _mutate()
    monkeypatch.setattr(mutate, "MUTATE_TMP", tmp_path / "kassalapp-mcp-mutate-0000aaaa")
    real_mkdtemp = mutate.tempfile.mkdtemp

    def with_a_lock_already_there(*args, **kwargs):
        made = pathlib.Path(real_mkdtemp(*args, **kwargs))
        (made / mutate.LOCK).write_text("", encoding="utf-8")
        return str(made)

    monkeypatch.setattr(mutate.tempfile, "mkdtemp", with_a_lock_already_there)
    with pytest.raises(FileExistsError):
        mutate.start_run_folder()


def test_a_sweep_goes_only_into_folders_that_are_really_ours(tmp_path, monkeypatch):
    """Another checkout's folder of runs that others may enter stops the run, untouched."""
    mutate = _mutate()
    monkeypatch.setattr(mutate, "MUTATE_TMP", tmp_path / "kassalapp-mcp-mutate-0000aaaa")
    other = tmp_path / "kassalapp-mcp-mutate-0000bbbb"
    left = _run_folder(other, "run-000000000004")
    other.chmod(0o755)
    with pytest.raises(SystemExit, match=f"chmod 700 {other}"):
        mutate.sweep()
    assert left.is_dir()


def test_a_sweep_stops_at_a_link_in_place_of_the_run_s_folder(tmp_path, monkeypatch):
    mutate = _mutate()
    place = tmp_path / "kassalapp-mcp-mutate-0000aaaa"
    monkeypatch.setattr(mutate, "MUTATE_TMP", place)
    elsewhere = tmp_path / "elsewhere"
    left = _run_folder(elsewhere, "run-000000000005")
    place.symlink_to(elsewhere)
    with pytest.raises(SystemExit, match=f"rm -r -- {place}"):
        mutate.sweep()
    assert left.is_dir()


def _mutate_on(tmp_path, monkeypatch, argv):
    """The mutation runner, pointed at an empty checkout in `tmp_path`."""
    mutate = _mutate()
    monkeypatch.setattr(mutate, "ROOT", tmp_path)
    monkeypatch.setattr(mutate, "ROWS", tmp_path / "scripts/mutations")
    monkeypatch.setattr(mutate, "RECOVERY", tmp_path / "recovery.json")
    monkeypatch.setattr(mutate, "MUTATE_TMP", tmp_path / "kassalapp-mcp-mutate-0000aaaa")
    monkeypatch.setattr(sys, "argv", ["mutate.py", *argv])
    (tmp_path / "code.py").write_text("x = 2\n", encoding="utf-8")
    left = {"file": "code.py", "mutated": "x = 2\n", "original": "x = 1\n"}
    mutate.RECOVERY.write_text(__import__("json").dumps(left), encoding="utf-8")
    return mutate


def test_only_one_mutation_run_goes_at_a_time_in_a_checkout(tmp_path, monkeypatch):
    """A second run would repair, sweep and mutate the files the first is working on."""
    mutate = _mutate_on(tmp_path, monkeypatch, [])
    first = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        __import__("fcntl").flock(first, __import__("fcntl").LOCK_EX)
        with pytest.raises(SystemExit, match="already going"):
            mutate.main()
    finally:
        os.close(first)
    assert (tmp_path / "code.py").read_text(encoding="utf-8") == "x = 2\n", "the first run's file was touched"
    assert mutate.RECOVERY.exists()


def test_repair_only_puts_back_what_a_killed_run_left_and_nothing_else(tmp_path, monkeypatch):
    mutate = _mutate_on(tmp_path, monkeypatch, ["--repair-only"])
    assert mutate.main() == 0
    assert (tmp_path / "code.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not mutate.RECOVERY.exists()
    assert not mutate.MUTATE_TMP.exists(), "it swept or started a run"
