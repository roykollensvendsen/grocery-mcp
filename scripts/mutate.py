#!/usr/bin/env python3
"""Turn every rule off in turn and see which tests notice.

Green on a first run proves nothing: a test that has never been red tests
nothing. Where a rule is written before its test, the test is run red first.
Where a rule already exists, this is the substitute. Every rule has one row,
in a file of its own under scripts/mutations/. Run it from the repository root:

    uv run python scripts/mutate.py [--write evidence/tests/rule-mutations.md]

A kill is not enough on its own. A mutation breaks whatever it breaks, and it
can break a test for an incidental reason — a documented example whose output
happened to change — leaving a rule looking guarded when nothing guards it. So
this also checks that the test named after the rule is one of the ones that went
red, and fails the run when it is not. `tests/test_rules_have_evidence.py` is
what requires that test to exist in the first place.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = pathlib.Path(__file__).resolve().parent.parent
#: One row per file: scripts/mutations/<the row's `file`, extension included>/<the
#: rule's words>.toml. Every pull request that added a row at the end of one shared
#: table collided with every other; a file per row only meets another pull request
#: adding the same rule.
ROWS = ROOT / "scripts/mutations"
#: Where the one mutated file is recorded while it is mutated. A `finally` does
#: not run when the process is killed, so without this a hard stop leaves a
#: source file switched off and the next person wondering why the suite is red.
RECOVERY = ROOT / "scripts/.mutate-recovery.json"
#: Where each suite run keeps its temporary files, one folder per run, removed when it
#: ends. Outside the checkout, where a test making a git repository of its own would
#: find this one instead; named after the checkout, so two never sweep each other's.
MUTATE_TMP = pathlib.Path(tempfile.gettempdir()) / (
    "grocery-mcp-mutate-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:8]
)
#: The command that runs the suite. A test may put a quicker program in its place, to
#: see how a run is ended without waiting for pytest to start in a scratch project.
SUITE = (sys.executable, "-m", "pytest")


def test_name_for(rule: str) -> str:
    """The test name a rule requires: its own words, as an identifier.

    Imported by ``tests/test_rules_have_evidence.py`` so the rule is written
    once. A rule named "a spoken reply is capped" wants a test called
    ``test_a_spoken_reply_is_capped``.
    """
    words = re.sub(r"[^a-z0-9]+", "_", rule.lower())
    return "test_" + re.sub(r"_+", "_", words).strip("_")


def row_path(rule: dict[str, Any]) -> pathlib.PurePosixPath:
    """Where a rule's row belongs, under ``ROWS``: its source file's whole path, its own words.

    A rule in ``src/grocery_mcp/server.py`` named "a 429 stops calls" belongs at
    ``src/grocery_mcp/server.py/a-429-stops-calls.toml``.
    """
    words = re.sub(r"[^a-z0-9]+", "-", str(rule["name"]).lower()).strip("-")
    if not words:
        msg = f"the rule {rule['name']!r} has no letter or digit to file its row under; reword it"
        raise SystemExit(msg)
    return pathlib.PurePosixPath(rule["file"]) / f"{words}.toml"


def _rows_in(path: pathlib.Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = tomllib.loads(path.read_text()).get("rule", [])
    return rows


def all_rows(folder: pathlib.Path | None = None) -> list[dict[str, Any]]:
    """Every row, one file each, in path order."""
    folder = ROWS if folder is None else folder
    found: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(folder.rglob("*.toml")) if folder.is_dir() else []:
        found += [(f"{folder.name}/{path.relative_to(folder).as_posix()}", rule) for rule in _rows_in(path)]
    where: dict[str, str] = {}
    for place, rule in found:
        if rule["name"] in where:
            msg = (
                f"the rule {rule['name']!r} has two rows, in {where[rule['name']]} and in {place};"
                f" delete one and keep {folder.name}/{row_path(rule)}"
            )
            raise SystemExit(msg)
        where[rule["name"]] = place
    return [rule for _, rule in found]


def misfiled(folder: pathlib.Path | None = None) -> list[str]:
    """Row files that hold other than one row, or sit where their rule does not put them."""
    folder = ROWS if folder is None else folder
    found = []
    for path in sorted(folder.rglob("*.toml")) if folder.is_dir() else []:
        here = pathlib.PurePosixPath(path.relative_to(folder).as_posix())
        rows = _rows_in(path)
        if len(rows) != 1:
            found.append(f"{here} holds {len(rows)} rows; give each rule a file of its own")
        elif here != (belongs := row_path(rows[0])):
            found.append(f"{here} should be {belongs}; move it there")
    return found


#: A rule's mark in the code: ``# RULE: what it says``.
MARKER = re.compile(r"^[ \t]*#\s*RULE:\s*(.+?)\s*$")


def marked() -> list[str]:
    """Every rule the code marks, in src/ and in the scripts, by its words."""
    sources = [*(ROOT / "src").rglob("*.py"), *(p for p in (ROOT / "scripts").glob("*.py") if p.is_file())]
    lines = [line for source in sorted(sources) for line in source.read_text(encoding="utf-8").splitlines()]
    return [match.group(1) for line in lines if (match := MARKER.search(line))]


def line_of(text: str, pattern: str, occurrence: int) -> int:
    """Find the one line a rule lives on, by pattern rather than by number."""
    seen = 0
    for i, line in enumerate(text.splitlines()):
        if re.search(pattern, line):
            seen += 1
            if seen == occurrence:
                return i
    msg = f"no line {occurrence} matching {pattern!r}"
    raise SystemExit(msg)


def _open_up(place: pathlib.Path) -> None:
    """Give the owner back every right on one folder or file, adding to the rights it has.

    A link is left alone: changing its rights would change whatever it points to,
    which may be outside the run's folder.
    """
    if place.is_symlink():
        return
    with contextlib.suppress(OSError):
        place.chmod(place.stat().st_mode | stat.S_IRWXU)


def _writable_again(func: Callable[[str], object], path: str, _exc: object) -> None:
    """Let a refused delete through: open up the path and its folder, then try again."""
    for place in (pathlib.Path(path).parent, pathlib.Path(path)):
        _open_up(place)
    func(path)


def _remove(folder: pathlib.Path) -> None:
    """Delete a run's folder, even where a test took away the rights to read or delete it."""
    if not folder.exists():
        return
    # A folder a test shut (mode 0) cannot even be listed, so open every folder first;
    # the handler then covers anything that still refuses.
    for place, folders, _ in os.walk(folder):
        for name in folders:
            _open_up(pathlib.Path(place) / name)
    shutil.rmtree(folder, onexc=_writable_again)


#: A run's folder holds its lock file, and the folder its suite's temporary files go in.
#: The lock sits beside that folder: pytest empties the folder it is given as it starts.
LOCK = ".lock"
#: A new run makes its lock, and never takes one already there.
# RULE: a new run s lock is always one it made
OPEN_LOCK = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
#: A sweep only opens a lock that is there: one it made would sit in a new run's folder
#: before that run moves in, and the run's rename onto it would then fail.
OPEN_THEIR_LOCK = os.O_RDWR | os.O_NOFOLLOW
#: A run's folder, and one being made, which is locked before it takes a run's name.
RUN_NAME = re.compile(r"run-[\w-]+")
STARTING = re.compile(r"starting-\w+")
#: How old a folder with no lock held, or none made yet, must be before it counts as left
#: by a killed run. A guess: a run takes well under a second to start and lock its folder,
#: and nothing measured says how long a run killed while starting may sit before then.
STARTING_SECONDS = 3600


def _looked_at(path: pathlib.Path) -> os.stat_result | None:
    """What `path` is, if it is a real folder of this user's; a link is never followed.

    None when it is not, or when another sweep removed it first.
    """
    try:
        found = path.lstat()
    except FileNotFoundError:
        return None
    return found if stat.S_ISDIR(found.st_mode) and found.st_uid == os.getuid() else None


def _ours(path: pathlib.Path) -> bool:
    """A real folder of this user's; a link is never followed."""
    return _looked_at(path) is not None


def _checkouts() -> list[pathlib.Path]:
    """Every checkout's folder of runs: this one's, and those of other checkouts and copies."""
    named = re.compile(re.escape(MUTATE_TMP.name[:-8]) + r"[0-9a-f]{8}")
    parent = MUTATE_TMP.parent
    others = [
        p for p in (parent.iterdir() if parent.is_dir() else []) if named.fullmatch(p.name) and _ours(p)
    ]
    return [MUTATE_TMP] * _ours(MUTATE_TMP) + [p for p in others if p != MUTATE_TMP]


def _dead(run: pathlib.Path) -> bool:
    """Whether `run` is a run's folder whose run has ended: nobody holds its lock."""
    starting = STARTING.fullmatch(run.name)
    found = _looked_at(run) if starting or RUN_NAME.fullmatch(run.name) else None
    if found is None:
        return False
    young = time.time() - found.st_mtime < STARTING_SECONDS
    if starting and young:
        return False  # it may be being made right now
    try:
        lock = os.open(run / LOCK, OPEN_THEIR_LOCK)
    except FileNotFoundError:
        # No lock yet: a new run's claimed name, or a run killed before it locked. The
        # first look's age is used: another sweep may have removed the folder since.
        # RULE: a claimed folder with no lock yet is never swept
        return not young
    except OSError:
        return False
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # RULE: a run still going is never swept
            return False  # a run still going holds its lock
    finally:
        os.close(lock)
    return True


def sweep() -> None:
    """Remove what runs killed before they could clean up left behind, in any checkout.

    A copy the quality check made is never run in again, so only another checkout's
    run comes by to sweep it. A run still going holds a lock, and is left alone.
    """
    if os.path.lexists(MUTATE_TMP):
        check_ours(MUTATE_TMP)  # a link at its name is otherwise passed over, and used later
    # RULE: a dead run's folder is swept in any checkout
    for checkout in _checkouts():
        # RULE: a sweep goes only into folders that are really ours
        check_ours(checkout)
        # RULE: a killed run's temporary files are swept at the next start
        for left in list(checkout.iterdir()):
            if _dead(left):
                _remove(left)
        with contextlib.suppress(OSError):  # rmdir takes it only when empty
            checkout.rmdir()


def _not_ours(folder: pathlib.Path, why: str, fix: str) -> str:
    return (
        f"{folder} {why}. Its name is easy to guess in a shared /tmp, so this run will not use it."
        f" {fix}, then run again."
    )


def check_ours(folder: pathlib.Path) -> None:
    """Stop unless `folder` is a real folder of this user's that no one else may enter.

    A folder of runs has a name anyone can guess, in a /tmp everyone shares. Nothing
    planted there first may steer where a run's files go, or what a sweep removes.
    """
    named = shlex.quote(str(folder))
    look = f"Look at it with: ls -ld {named}. If it is not yours, remove it with: rm -r -- {named}"
    try:
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:  # a link, or something else, planted at the name
        raise SystemExit(_not_ours(folder, f"is not a folder of yours ({error.strerror})", look)) from None
    try:
        held = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISDIR(held.st_mode) or held.st_uid != os.getuid():
        raise SystemExit(_not_ours(folder, "is not a folder of yours", look))
    if held.st_mode & 0o077:
        mode = stat.S_IMODE(held.st_mode)
        raise SystemExit(
            _not_ours(folder, f"is one others may enter (mode {mode:o})", f"Close it with: chmod 700 {named}")
        )


def own_folder() -> None:
    """Make this checkout's folder of runs, for this user alone, or check the one there is."""
    MUTATE_TMP.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        MUTATE_TMP.mkdir(mode=0o700)
    # RULE: a run trusts its folder only when it is really ours
    check_ours(MUTATE_TMP)


def start_run_folder() -> tuple[pathlib.Path, int]:
    """A new run's folder, and the lock this run now holds on it, taken before it has a run's name."""
    own_folder()
    starting = pathlib.Path(tempfile.mkdtemp(prefix="starting-", dir=MUTATE_TMP))
    lock = os.open(starting / LOCK, OPEN_LOCK, 0o600)
    # RULE: a new run's folder is locked before it takes a run's name
    fcntl.flock(lock, fcntl.LOCK_EX)
    while True:
        folder = MUTATE_TMP / f"run-{secrets.randbelow(10**12):012d}"
        try:
            folder.mkdir(mode=0o700)  # claims the name, so the rename replaces no one else's
        except FileExistsError:
            continue
        starting.rename(folder)
        return folder, lock


#: The stops from outside: Ctrl-C, a cancelled CI job (GitHub sends SIGINT, then SIGTERM),
#: and a closed terminal.
STOPS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def stop_cleanly_when_killed() -> None:
    """Make every stop from outside end this script as Ctrl-C does, so every clean-up runs.

    Python runs ``finally`` blocks and removes temporary folders on Ctrl-C, but a plain
    ``kill`` or a closed terminal ended it at once, leaving them behind. After the first
    stop, the rest are ignored, so the clean-up runs to its end.
    """

    def stop(signum: int, _frame: object) -> None:
        # RULE: a second stop does not cut the cleanup short
        for each in STOPS:
            signal.signal(each, signal.SIG_IGN)
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    # RULE: a stopped mutation run still puts everything back
    for each in STOPS:
        signal.signal(each, stop)


def start() -> None:
    """Get ready to run: stop cleanly if killed, and clear up after a run that was not."""
    stop_cleanly_when_killed()
    repair()
    sweep()


def repair() -> None:
    """Put back a file an earlier run was killed in the middle of mutating."""
    if not RECOVERY.is_file():
        return
    kept = json.loads(RECOVERY.read_text())
    path = ROOT / kept["file"]
    if path.read_text() == kept["mutated"]:
        path.write_text(kept["original"])
        print(f"repaired {kept['file']}, left mutated by a run that was killed")  # noqa: T201
    else:
        print(f"{kept['file']} was left mutated and has since changed; not touching it")  # noqa: T201
    RECOVERY.unlink()


def restore(path: pathlib.Path, original: str, mutated: str) -> bool:
    """Put a file back, unless somebody else changed it while the suite ran."""
    if path.read_text() != mutated:
        # Restoring now would overwrite an edit made while this was running,
        # which is a silent way to lose somebody's work.
        # RULE: a file changed during the run is never restored over
        return False
    path.write_text(original)
    return True


#: Colour codes pytest writes around FAILED, which a line-anchored pattern
#: would otherwise never match.
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def run_suite(timeout: float | None = None) -> tuple[int, list[str]]:
    """Run the tests and give back how many failed, and which ones.

    A real run passes no `timeout` and has no limit, since runs never have one. Only a
    test's own scratch run passes one, so a suite that hangs fails that test instead.
    """
    # Two mutations that replace equal-length text leave files of identical
    # size, and CPython validates a cached .pyc from the source's modification
    # time and size -- both at one-second resolution. Inside the same second
    # the next run would import the previous mutation's bytecode and measure
    # the wrong rule, so no bytecode is written at all.
    # RULE: two mutations never share a bytecode cache
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    temporary, lock = start_run_folder()
    try:
        with subprocess.Popen(  # noqa: S603 - this interpreter, pytest, and a folder this script made
            [
                *SUITE,
                "-q",
                "--tb=no",
                "--color=no",
                "-p",
                "no:cacheprovider",
                f"--basetemp={temporary / 'basetemp'}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=ROOT,
            env=env,
            # A bounded run gets a group of its own, so an overrun can end all it started.
            start_new_session=timeout is not None,
        ) as suite:
            try:
                # RULE: a real mutation run has no time limit
                stdout, _ = suite.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Ending only pytest would leave what its tests started, still holding the
                # output pipe, so the wait for that pipe would last as long as they do.
                # RULE: a scratch run that overruns leaves nothing it started running
                os.killpg(suite.pid, signal.SIGKILL)
                suite.communicate()
                raise
            except BaseException:
                # Stopped any other way, the run ends its suite before it puts the files back.
                # RULE: a suite run that is stopped ends its suite
                suite.kill()
                raise
    finally:
        # RULE: a suite run leaves no temporary files behind
        _remove(temporary)
        os.close(lock)
        # The run's own folder goes too, once no other run in this checkout is using it.
        # RULE: a mutation run leaves no folder of its own behind
        with contextlib.suppress(OSError):
            MUTATE_TMP.rmdir()
    # --color=no covers the usual run; stripping covers a configuration that
    # turns colour back on, which would otherwise make every kill unattributable.
    # RULE: a coloured failure line is still attributed
    plain = ANSI.sub("", stdout)
    m = re.search(r"(\d+) (?:failed|error)", plain)
    failed = re.findall(r"^FAILED (\S+)", plain, re.MULTILINE)
    return int(m.group(1)) if m else 0, [f.split("::")[-1].split("[")[0] for f in failed]


def nothing_to_check(rules: list[dict[str, Any]]) -> int | None:
    """The exit code when there are no rows to run, or None when there are."""
    # RULE: an empty rule table is not a crash
    # No rules and no rows passes. Outside a test this is hard to reach: a fresh
    # scaffold ships its own tools' rules, so it ships their rows too, and the
    # next check fails closed.
    if not rules and not marked():
        print(f"no rules in {ROWS.relative_to(ROOT)} yet; add one file per rule the code enforces")  # noqa: T201
        return 0
    # RULE: rules with no rows are a failure not a pass
    # A check that passes with nothing to check is fail-open: a folder moved or
    # emptied by mistake would leave every rule unproven and the run green.
    if not rules:
        print(  # noqa: T201
            f"nothing to switch off: the code marks rules, but {ROWS.relative_to(ROOT)} has no rows."
            " Restore it from main (git checkout origin/main -- scripts/mutations), or add a row per rule."
        )
        return 1
    return None


def hold_checkout() -> int:
    """Hold this checkout for one run, or stop: a second would mutate the first one's files."""
    held = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # RULE: only one mutation run goes at a time in a checkout
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(held)
        busy = f"a mutation run is already going in {ROOT}. Wait for it to end, then run again."
        raise SystemExit(busy) from None
    return held


def main() -> int:
    """Hold the checkout, then check every rule, or only put back what a killed run left."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", type=pathlib.Path, help="write the evidence table here")
    parser.add_argument(
        "--repair-only", action="store_true", help="only put back a file a killed run left mutated"
    )
    args = parser.parse_args()
    held = hold_checkout()
    try:
        if args.repair_only:
            repair()
            return 0
        return check_every_rule(args.write)
    finally:
        os.close(held)


def check_the_package() -> None:
    """Stop unless the suite will import this checkout's own package.

    Two worktrees sharing one venv import whichever one installed the package last, so
    every rule would be measured against the other one's code. The check runs in a fresh
    interpreter, the one the suite will run in, so nothing this script imported can fool it.
    """
    source = (ROOT / "src").resolve()
    packages = sorted(p.parent.name for p in source.glob("*/__init__.py"))
    if not packages:
        empty = (
            f"There is no package in {source} to check, so this run would measure nothing of its own."
            " Run it from the plugin's checkout, where src/grocery_mcp/ is."
        )
        # RULE: a mutation run refuses a checkout with no package
        raise SystemExit(empty)
    for package in packages:
        found = subprocess.run(  # noqa: S603 - this interpreter, importing a package of this checkout
            [sys.executable, "-c", f"import {package}; print({package}.__file__)"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        where = pathlib.Path(found.stdout.strip()).resolve().parent if found.returncode == 0 else None
        if where is None or not where.is_relative_to(source):
            place = where or f"nowhere ({found.stderr.strip()[-200:]})"
            refusal = (
                f"The suite would import {package} from {place}, not from this checkout's {source}."
                f" Run `uv sync` in {ROOT}, and do not point UV_PROJECT_ENVIRONMENT at a shared venv."
            )
            # RULE: a mutation run refuses a package from another checkout
            raise SystemExit(refusal)


def check_every_rule(write: pathlib.Path | None) -> int:
    """Mutate every rule in turn, restore the tree, and report."""
    start()
    check_the_package()
    rules = all_rows()
    if (empty := nothing_to_check(rules)) is not None:
        return empty
    results = []
    lost: list[str] = []
    bad_rows: list[str] = []
    standing: tuple[pathlib.Path, str, str] | None = None
    try:
        for rule in rules:
            path = ROOT / rule["file"]
            # Read each file as it is now, not as it was when the run began: a
            # bulk snapshot taken up front is restored over anything edited
            # since, and losing an edit that way costs more than the run.
            text = path.read_text()
            mutated = switched_off(text, rule)
            why = broken(rule["file"], mutated)
            # RULE: a row that breaks the code is reported as broken
            if why:
                bad_rows.append(rule["name"])
                print(f"BROKEN   {rule['name']}: the replacement is not valid code ({why})", flush=True)  # noqa: T201
                continue
            RECOVERY.write_text(json.dumps({"file": rule["file"], "original": text, "mutated": mutated}))
            path.write_text(mutated)
            standing = (path, text, mutated)
            # Sent at once, not held until the run ends: in CI the output is a pipe, so a
            # run that hung would otherwise leave no word of which rule it hung on.
            # RULE: a mutation run names each rule before it tests it
            print(f"testing  {rule['name']}", flush=True)  # noqa: T201
            began = time.monotonic()
            count, failed = run_suite()
            # RULE: a mutation run says how long each rule took
            took = time.monotonic() - began
            if not restore(path, text, mutated):
                lost.append(rule["file"])
            standing = None
            RECOVERY.unlink(missing_ok=True)
            # A kill by some other test is not evidence about this rule.
            own = test_name_for(rule["name"])
            by_its_own = own in failed
            results.append((rule["name"], count, own, by_its_own))
            verdict = "KILLED  " if by_its_own else ("BY LUCK " if count else "SURVIVED")
            print(f"{verdict} {rule['name']}: {count} in {took:.0f} s", flush=True)  # noqa: T201
    finally:
        if standing is not None:
            restore(*standing)
        RECOVERY.unlink(missing_ok=True)
    return _summary(results, bad_rows, lost, write)


def _summary(
    results: list[tuple[str, int, str, bool]],
    bad_rows: list[str],
    lost: list[str],
    write: pathlib.Path | None,
) -> int:
    """Say what the run found, write the evidence page if asked, and give the exit status."""
    survived = [name for name, count, _, _ in results if count == 0]
    lucky = [name for name, count, _, own in results if count and not own]
    proven = len(results) - len(survived) - len(lucky)
    print(f"\n{proven}/{len(results)} rules turned off the test that names them")  # noqa: T201
    if survived:
        print("SURVIVED, so nothing protects them:", ", ".join(survived))  # noqa: T201
    if lucky:
        print("KILLED BY ANOTHER TEST, so the evidence is not about them:", ", ".join(lucky))  # noqa: T201
    if bad_rows:
        print("BROKEN ROW, so nothing was tested; make each a whole valid line:", ", ".join(bad_rows))  # noqa: T201
        # In CI the job's own page says it too, so nobody has to read the log to find the row.
        if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with pathlib.Path(step_summary).open("a", encoding="utf-8") as page:
                page.write(_broken_line(bad_rows) + "\n")
    if lost:
        print("CHANGED WHILE MUTATED, so left as they are:", ", ".join(sorted(set(lost))))  # noqa: T201
        print("Edit the tree while this runs and the result is not about your code.")  # noqa: T201
    if write:
        write.write_text(report(results, survived, lucky, bad_rows))
    return 1 if survived or lucky or lost or bad_rows else 0


def switched_off(text: str, rule: dict[str, Any]) -> str:
    """The file's text with the rule's whole line replaced, keeping its indentation."""
    lines = text.splitlines()
    i = line_of(text, rule["find"], rule.get("occurrence", 1))
    # Keep the line's own indentation: a replacement that carries its own
    # would have to be kept in step with the source, and silently would not be.
    indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
    lines[i] = indent + rule["replace"].strip()
    return "\n".join(lines) + "\n"


def broken(file: str, mutated: str) -> str | None:
    """Why a mutated Python file no longer compiles, or None when it does.

    A replacement cut off mid-expression stops the module importing, so every
    test in its files errors: the run would count the rule as caught when the
    row itself is wrong.
    """
    if not file.endswith(".py"):
        return None
    try:
        compile(mutated, file, "exec")
    except SyntaxError as e:
        return f"line {e.lineno}: {e.msg}"
    return None


def _broken_line(bad_rows: list[str]) -> str:
    return f"Broken rows, so nothing was tested: {', '.join(bad_rows)}."


def report(
    results: list[tuple[str, int, str, bool]],
    survived: list[str],
    lucky: list[str],
    bad_rows: list[str],
) -> str:
    """Render the run as the evidence page."""
    rows = "\n".join(
        f"| {name} | {count} | `{own}` | {'yes' if by_own else 'NO'} |"
        for name, count, own, by_own in results
    )
    proven = len(results) - len(survived) - len(lucky)
    verdict = (
        f"**Result: {proven} of {len(results)} rules turned off the test that names them.**"
        + (" None survived." if not survived else f" Survived: {', '.join(survived)}.")
        + ("" if not lucky else f" Killed only by other tests: {', '.join(lucky)}.")
        + ("" if not bad_rows else " " + _broken_line(bad_rows))
    )
    return f"""# Every rule, turned off in turn

*Run {dt.datetime.now(dt.UTC).date()} by `python scripts/mutate.py --write
evidence/tests/rule-mutations.md`, which is how to repeat it. The script
restores every file it touched before it exits.*

Green on a first run proves nothing: a test that has never been red tests
nothing. Where a rule is written before its test, the test is run red first.
Where a rule already exists, this is the substitute. Each rule is disabled in
turn, by replacing its one line with something that can never hold, and the
suite is run. A rule whose removal breaks no test is a rule nothing protects.

{verdict}

| Rule turned off | Tests that went red | The test that names it | Did that one go red? |
|---|---|---|---|
{rows}

## What this does not prove

That the rules are the right rules, or that a rule catches every case of what
it names. It proves each rule is load-bearing: switch it off and a named test
says so. The last column is why that is worth more than a count: a mutation can
break a test for an incidental reason, and a `NO` there fails this script even
though something went red. A new rule follows the order in `CONTRIBUTING.md`
instead, a failing test first, and gets a row under `scripts/mutations/`, which
`tests/test_rules_have_evidence.py` requires.
"""


if __name__ == "__main__":
    sys.exit(main())
