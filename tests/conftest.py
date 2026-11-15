"""Checks over the whole test run, and what every test must leave as it found it.

The run fails if a test changed the rule check's real recovery file, and the
three stop signals are put back after every test.

The recovery file:

`scripts/mutate.py` keeps the file it is mutating there while the suite runs. A
test that ran the runner without its own `RECOVERY` would overwrite or remove it,
and in the middle of a mutation run the original would be lost. Proven by
`tests/test_real_recovery_file_is_guarded.py`.
"""

import pathlib
import signal

import pytest

ROOT = pathlib.Path(__file__).parent.parent
RECOVERY = ROOT / "scripts/.mutate-recovery.json"
BEFORE = pytest.StashKey[bytes | None]()


def _now() -> bytes | None:
    return RECOVERY.read_bytes() if RECOVERY.is_file() else None


def pytest_sessionstart(session: pytest.Session) -> None:
    session.config.stash[BEFORE] = _now()


def pytest_sessionfinish(session: pytest.Session) -> None:
    if _now() != session.config.stash[BEFORE]:
        print(  # noqa: T201
            "\nA test changed scripts/.mutate-recovery.json, the rule check's real recovery file. "
            "A test that runs scripts/mutate.py must point RECOVERY at its own temporary file."
        )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


#: The stops the scripts take over, so a kill cleans up as Ctrl-C does.
STOPS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@pytest.fixture(autouse=True)
def _stops_as_they_were():
    """A test that calls a script's main() installs its stop handlers here; put them back.

    Left in place, a kill would no longer end this test run, and after one stop the
    rest would be ignored. Proven by `tests/test_stopped_runs_clean_up.py`.
    """
    before = {each: signal.getsignal(each) for each in STOPS}
    yield
    for each, handler in before.items():
        signal.signal(each, handler)
