"""The mutation runner's own rules, each with a test named after it.

`scripts/mutate.py` proves every other rule, so its own rules are proven the same
way: `scripts/mutations/scripts/mutate.py/` switches each off in turn, and the test
here that names it goes red. The runner has already loaded its code when it switches
a line off, and the suite it starts loads the switched-off file, so it can prove itself.

None of these runs a real suite: the suite is a stand-in that records what it was
asked to do, so a test here never mutates anything. This follows garage-door's
pull requests 36 to 38.
"""

import ast
import importlib.util
import pathlib
import re
import subprocess
import types

import pytest

ROOT = pathlib.Path(__file__).parent.parent


@pytest.fixture
def mutate(tmp_path, monkeypatch):
    """The runner, loaded by path, with its temporary folder under `tmp_path`."""
    spec = importlib.util.spec_from_file_location("mutate_runner", ROOT / "scripts/mutate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "MUTATE_TMP", tmp_path / "mutate-tmp", raising=False)
    # These checkouts hold no package; the package check has tests of its own.
    monkeypatch.setattr(module, "check_the_package", lambda: None)
    return module


def _suite_says(monkeypatch, mutate, stdout, returncode=1):
    """Stand in for the suite: it answers `stdout`, and the arguments it got are kept."""
    asked = {}

    class Suite:
        def __init__(self, args, **kwargs):
            asked.update(args=args, **kwargs)
            self.returncode = returncode

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def communicate(self, timeout=None):
            asked["timeout"] = timeout
            return stdout, ""

    monkeypatch.setattr(mutate.subprocess, "Popen", Suite)
    return asked


def test_a_file_changed_during_the_run_is_never_restored_over(tmp_path, mutate):
    """Someone saved the file while the suite ran: putting the original back would lose their edit."""
    source = tmp_path / "answers.py"
    source.write_text("edited while the suite ran\n", encoding="utf-8")
    assert mutate.restore(source, "original\n", "mutated\n") is False
    assert source.read_text(encoding="utf-8") == "edited while the suite ran\n"


def test_two_mutations_never_share_a_bytecode_cache(monkeypatch, mutate):
    """Two equal-size mutations in one second would otherwise import the last one's bytecode."""
    # Unset here, so the runner has to set it itself: a mutation run sets it for the suite it starts.
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    asked = _suite_says(monkeypatch, mutate, "1 passed\n", returncode=0)
    mutate.run_suite()
    assert asked["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_a_real_mutation_run_has_no_time_limit(monkeypatch, mutate):
    """Runs are never cut short; only a test's own scratch run sets a bound."""
    asked = _suite_says(monkeypatch, mutate, "1 passed\n", returncode=0)
    mutate.run_suite()
    assert asked["timeout"] is None


def test_a_test_s_scratch_run_of_the_suite_is_bounded_by_the_test(monkeypatch, mutate):
    asked = _suite_says(monkeypatch, mutate, "1 passed\n", returncode=0)
    mutate.run_suite(timeout=300)
    assert asked["timeout"] == 300


def test_a_suite_run_that_is_stopped_ends_its_suite(monkeypatch, mutate):
    """A Ctrl-C while the suite runs must not leave it running while the files are put back."""
    ended = []

    class Suite:
        pid = 0

        def __init__(self, *_, **__):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def communicate(self, **_):
            raise KeyboardInterrupt

        def kill(self):
            ended.append(True)

    monkeypatch.setattr(mutate.subprocess, "Popen", Suite)
    with pytest.raises(KeyboardInterrupt):
        mutate.run_suite()
    assert ended == [True]


def _overrunning_suite(monkeypatch, mutate, *, drain_too=False):
    """Stand in for a suite whose first wait runs out of time; every later wait is noted.

    With `drain_too`, the wait after the group is ended runs out of time as well.
    """
    waits = []

    class Suite:
        pid = 0

        def __init__(self, *_, **__):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def communicate(self, timeout=None):
            waits.append(timeout)
            if len(waits) == 1 or drain_too:
                raise subprocess.TimeoutExpired(cmd="pytest", timeout=timeout)
            return "", ""

    monkeypatch.setattr(mutate.subprocess, "Popen", Suite)
    return waits


def test_an_overrun_whose_group_has_already_ended_is_still_reported_as_an_overrun(monkeypatch, mutate):
    """Ended on its own since the time ran out, the group cannot be ended again; that is no new error."""
    _overrunning_suite(monkeypatch, mutate)

    def gone(*_):
        raise ProcessLookupError

    monkeypatch.setattr(mutate.os, "killpg", gone)
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        mutate.run_suite(timeout=1)
    assert raised.value.timeout == 1


def test_the_wait_after_an_overrun_is_bounded(monkeypatch, mutate):
    """A program that left the group could hold the output open for ever; the run must not wait on it."""
    waits = _overrunning_suite(monkeypatch, mutate)
    monkeypatch.setattr(mutate.os, "killpg", lambda *_: None)
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        mutate.run_suite(timeout=1)
    assert waits == [1, mutate.DRAIN]
    assert raised.value.timeout == 1


def test_an_overrun_reports_its_own_time_limit_even_when_the_wait_after_it_runs_out(monkeypatch, mutate):
    """The short wait after an overrun may run out too; the run's own limit is still what is reported."""
    waits = _overrunning_suite(monkeypatch, mutate, drain_too=True)
    monkeypatch.setattr(mutate.os, "killpg", lambda *_: None)
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        mutate.run_suite(timeout=1)
    assert waits == [1, mutate.DRAIN]
    assert raised.value.timeout == 1


def test_a_coloured_failure_line_is_still_attributed(monkeypatch, mutate):
    """With colour on, FAILED is wrapped in codes, and a kill must still name its test."""
    coloured = (
        "\x1b[31mFAILED\x1b[0m tests/test_quota.py::test_a_429_stops_calls - assert\n"
        "\x1b[31m1 failed\x1b[0m\n"
    )
    _suite_says(monkeypatch, mutate, coloured)
    assert mutate.run_suite() == (1, ["test_a_429_stops_calls"])


def test_an_empty_rule_table_is_not_a_crash(tmp_path, monkeypatch, capsys, mutate):
    """A repository fresh from the scaffold has no rows yet, and is told what to add."""
    monkeypatch.setattr(mutate, "ROOT", tmp_path)
    monkeypatch.setattr(mutate, "ROWS", tmp_path / "scripts/mutations")
    monkeypatch.setattr(mutate, "RECOVERY", tmp_path / "recovery.json")
    monkeypatch.setattr("sys.argv", ["mutate.py"])
    assert mutate.main() == 0
    assert "add one file per rule" in capsys.readouterr().out


def _scaffold(tmp_path, monkeypatch, mutate, source):
    """A repository in `tmp_path` holding `source` as its only code, and no rows."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src/code.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(mutate, "ROOT", tmp_path)
    monkeypatch.setattr(mutate, "ROWS", tmp_path / "scripts/mutations")
    monkeypatch.setattr(mutate, "RECOVERY", tmp_path / "recovery.json")
    monkeypatch.setattr("sys.argv", ["mutate.py"])


def test_rules_with_no_rows_are_a_failure_not_a_pass(tmp_path, monkeypatch, capsys, mutate):
    """A rows folder moved or emptied by mistake would leave every rule unproven, and the run green."""
    _scaffold(tmp_path, monkeypatch, mutate, "# RULE: something is kept\nx = 1\n")
    assert mutate.main() == 1
    assert "git checkout origin/main -- scripts/mutations" in capsys.readouterr().out


def test_a_fresh_scaffold_with_no_rules_and_no_rows_passes(tmp_path, monkeypatch, mutate):
    _scaffold(tmp_path, monkeypatch, mutate, "x = 1\n")
    assert mutate.main() == 0


#: The function a killed run repairs with. A rule there would be switched off while it repairs.
UNPROVABLE = "repair"

REPAIR_WITH_A_RULE = '''
def repair():
    """Put back a file a killed run left mutated."""
    # RULE: a repaired file is the original
    if kept:
        write(kept)


def sweep():
    # RULE: a swept folder is gone
    remove()
'''


def _inside_repair(text, rows, mutate):
    """The rules marked, or switched off by a row, in `repair()` or any function of `text` it calls."""
    defs = (ast.FunctionDef, ast.AsyncFunctionDef)
    functions = {f.name: f for f in ast.parse(text).body if isinstance(f, defs)}
    assert UNPROVABLE in functions, (
        f"no {UNPROVABLE}() in the runner; if it was renamed, rename UNPROVABLE too"
    )
    reached, waiting = set(), [UNPROVABLE]
    while waiting:  # repair() and everything it calls, however deep, within this file
        name = waiting.pop()
        reached.add(name)
        calls = [
            c for c in ast.walk(functions[name]) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        ]
        waiting += [c for c in {c.func.id for c in calls} if c in functions and c not in reached]
    lines = text.splitlines()
    inside = {i for name in reached for i in range(functions[name].lineno - 1, functions[name].end_lineno)}
    marked = [lines[i].split("# RULE:")[1].strip() for i in sorted(inside) if "# RULE:" in lines[i]]
    rowed = [
        r["name"] for r in rows if mutate.line_of(text, r["find"], int(r.get("occurrence", 1))) in inside
    ]
    return sorted(set(marked + rowed))


def test_a_rule_inside_repair_is_found(mutate):
    """The guard below is only worth something if it sees a rule where one has been put."""
    rows = [
        {"name": "a swept folder is gone", "find": r"^\s*remove\(\)$"},
        {"name": "kept", "find": r"if kept:"},
    ]
    assert _inside_repair(REPAIR_WITH_A_RULE, rows, mutate) == ["a repaired file is the original", "kept"]


def test_no_rule_lives_inside_repair(mutate):
    """A killed run repairs with the code it left switched off, so a rule in repair() cannot be proven."""
    text = (ROOT / "scripts/mutate.py").read_text(encoding="utf-8")
    rows = [r for r in mutate.all_rows() if r["file"] == "scripts/mutate.py"]
    assert rows, "the runner's own rows are missing"
    assert _inside_repair(text, rows, mutate) == []


REPAIR_CALLING_A_RULE = """
def write(kept):
    # RULE: a repaired file is written whole
    path.write_text(kept)


def repair():
    write(kept)
"""


def test_a_rule_in_what_repair_calls_is_found(mutate):
    """A helper repair() calls runs switched off too, so its rule cannot be proven either."""
    assert _inside_repair(REPAIR_CALLING_A_RULE, [], mutate) == ["a repaired file is written whole"]


def test_a_runner_with_no_repair_is_refused_by_name(mutate):
    """Renamed, repair() would leave the guard checking nothing; it says what it looked for."""
    with pytest.raises(AssertionError, match=r"no repair\(\) in"):
        _inside_repair("def mend():\n    pass\n", [], mutate)


ASYNC_REPAIR_HELPER = """
async def write(kept):
    # RULE: a repaired file is written in the background
    path.write_text(kept)


def repair():
    write(kept)
"""


def test_a_rule_in_an_async_helper_repair_calls_is_found(mutate):
    """An async helper repair() calls runs switched off too."""
    assert _inside_repair(ASYNC_REPAIR_HELPER, [], mutate) == ["a repaired file is written in the background"]


def _one_rule(tmp_path, monkeypatch, mutate):
    """A repository with the one rule `x is one`, its row, and nothing else to set up."""
    _scaffold(tmp_path, monkeypatch, mutate, "# RULE: x is one\nx = 1\n")
    rows = tmp_path / "scripts/mutations/src/code.py"
    rows.mkdir(parents=True)
    (rows / "x-is-one.toml").write_text(
        "[[rule]]\nname = \"x is one\"\nfile = \"src/code.py\"\nfind = '^x = 1$'\nreplace = 'x = 2'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mutate, "start", lambda: None)


class _Pipe:
    """Output that, like a pipe in CI, shows only what has been flushed."""

    def __init__(self):
        self.held, self.shown = "", ""

    def write(self, text):
        self.held += text
        return len(text)

    def flush(self):
        self.shown, self.held = self.shown + self.held, ""


def test_a_mutation_run_names_each_rule_before_it_tests_it(tmp_path, monkeypatch, mutate):
    """The rule's name must be out of the process, not in its buffer, by the time the suite starts.

    In CI the output is a pipe, which Python holds back until it is full. A run that hung on
    one rule would leave nothing in the log to say which, so the result line is sent at once too.
    """
    _one_rule(tmp_path, monkeypatch, mutate)
    pipe, seen = _Pipe(), []
    monkeypatch.setattr(mutate.sys, "stdout", pipe)

    def suite():
        seen.append(pipe.shown)
        return 1, ["test_x_is_one"]

    monkeypatch.setattr(mutate, "run_suite", suite)
    mutate.check_every_rule(None)
    assert "testing  x is one\n" in seen[0], seen
    assert re.search(r"KILLED   x is one: 1 in \d+ s\n", pipe.shown), pipe.shown


def test_a_mutation_run_says_how_long_each_rule_took(tmp_path, monkeypatch, capsys, mutate):
    """The seconds are what show a slow rule, so they must be the suite's own time, not any number."""
    _one_rule(tmp_path, monkeypatch, mutate)
    clock = iter([10.0, 52.4])
    monkeypatch.setattr(mutate, "time", types.SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(mutate, "run_suite", lambda: (1, ["test_x_is_one"]))
    mutate.check_every_rule(None)
    assert "KILLED   x is one: 1 in 42 s\n" in capsys.readouterr().out


def test_a_row_that_breaks_the_code_is_reported_as_broken(tmp_path, monkeypatch, capsys, mutate):
    """A replacement that does not compile fails every test, which would pass for a kill."""
    _one_rule(tmp_path, monkeypatch, mutate)
    (tmp_path / "scripts/mutations/src/code.py/x-is-one.toml").write_text(
        "[[rule]]\nname = \"x is one\"\nfile = \"src/code.py\"\nfind = '^x = 1$'\nreplace = 'x = ('\n",
        encoding="utf-8",
    )
    suites = []
    monkeypatch.setattr(mutate, "run_suite", lambda: suites.append(1) or (1, ["test_x_is_one"]))
    summary, written = tmp_path / "step-summary.md", tmp_path / "rule-mutations.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert mutate.check_every_rule(written) == 1
    out = capsys.readouterr().out
    assert "BROKEN   x is one: the replacement is not valid code" in out
    assert "KILLED" not in out
    assert suites == []
    assert (tmp_path / "src/code.py").read_text(encoding="utf-8") == "# RULE: x is one\nx = 1\n"
    assert "Broken rows, so nothing was tested: x is one." in written.read_text(encoding="utf-8")
    assert "Broken rows, so nothing was tested: x is one." in summary.read_text(encoding="utf-8")
