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
    "kassalapp-mcp-mutate-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:8]
)


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

    A rule in ``src/kassalapp_mcp/server.py`` named "a 429 stops calls" belongs at
    ``src/kassalapp_mcp/server.py/a-429-stops-calls.toml``.
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
OPEN_LOCK = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
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
    # RULE: a dead run's folder is swept in any checkout
    for checkout in _checkouts():
        # RULE: a killed run's temporary files are swept at the next start
        for left in list(checkout.iterdir()):
            if _dead(left):
                _remove(left)
        with contextlib.suppress(OSError):  # rmdir takes it only when empty
            checkout.rmdir()


def start_run_folder() -> tuple[pathlib.Path, int]:
    """A new run's folder, and the lock this run now holds on it, taken before it has a run's name."""
    MUTATE_TMP.mkdir(parents=True, exist_ok=True)
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


def run_suite() -> tuple[int, list[str]]:
    """Run the tests and give back how many failed, and which ones."""
    # Two mutations that replace equal-length text leave files of identical
    # size, and CPython validates a cached .pyc from the source's modification
    # time and size -- both at one-second resolution. Inside the same second
    # the next run would import the previous mutation's bytecode and measure
    # the wrong rule, so no bytecode is written at all.
    # RULE: two mutations never share a bytecode cache
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    temporary, lock = start_run_folder()
    try:
        r = subprocess.run(  # noqa: S603 - this interpreter, pytest, and a folder this script made
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--tb=no",
                "--color=no",
                "-p",
                "no:cacheprovider",
                f"--basetemp={temporary / 'basetemp'}",
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
            env=env,
        )
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
    plain = ANSI.sub("", r.stdout)
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


def main() -> int:
    """Mutate every rule in turn, restore the tree, and report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", type=pathlib.Path, help="write the evidence table here")
    args = parser.parse_args()

    start()
    rules = all_rows()
    if (empty := nothing_to_check(rules)) is not None:
        return empty
    results = []
    lost: list[str] = []
    standing: tuple[pathlib.Path, str, str] | None = None
    try:
        for rule in rules:
            path = ROOT / rule["file"]
            # Read each file as it is now, not as it was when the run began: a
            # bulk snapshot taken up front is restored over anything edited
            # since, and losing an edit that way costs more than the run.
            text = path.read_text()
            lines = text.splitlines()
            i = line_of(text, rule["find"], rule.get("occurrence", 1))
            # Keep the line's own indentation: a replacement that carries its own
            # would have to be kept in step with the source, and silently would not be.
            indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
            lines[i] = indent + rule["replace"].strip()
            mutated = "\n".join(lines) + "\n"
            RECOVERY.write_text(json.dumps({"file": rule["file"], "original": text, "mutated": mutated}))
            path.write_text(mutated)
            standing = (path, text, mutated)
            count, failed = run_suite()
            if not restore(path, text, mutated):
                lost.append(rule["file"])
            standing = None
            RECOVERY.unlink(missing_ok=True)
            # A kill by some other test is not evidence about this rule.
            own = test_name_for(rule["name"])
            by_its_own = own in failed
            results.append((rule["name"], count, own, by_its_own))
            verdict = "KILLED  " if by_its_own else ("BY LUCK " if count else "SURVIVED")
            print(f"{verdict} {rule['name']}: {count}")  # noqa: T201
    finally:
        if standing is not None:
            restore(*standing)
        RECOVERY.unlink(missing_ok=True)

    survived = [name for name, count, _, _ in results if count == 0]
    lucky = [name for name, count, _, own in results if count and not own]
    proven = len(results) - len(survived) - len(lucky)
    print(f"\n{proven}/{len(results)} rules turned off the test that names them")  # noqa: T201
    if survived:
        print("SURVIVED, so nothing protects them:", ", ".join(survived))  # noqa: T201
    if lucky:
        print("KILLED BY ANOTHER TEST, so the evidence is not about them:", ", ".join(lucky))  # noqa: T201
    if lost:
        print("CHANGED WHILE MUTATED, so left as they are:", ", ".join(sorted(set(lost))))  # noqa: T201
        print("Edit the tree while this runs and the result is not about your code.")  # noqa: T201
    if args.write:
        args.write.write_text(report(results, survived, lucky))
    return 1 if survived or lucky or lost else 0


def report(results: list[tuple[str, int, str, bool]], survived: list[str], lucky: list[str]) -> str:
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
