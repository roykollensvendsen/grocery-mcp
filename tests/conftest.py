"""Checks that hold over the whole test session, not one test."""

import pathlib

import pytest

ROOT = pathlib.Path(__file__).parent.parent
#: Where the mutation check records the one file it has switched off, so a killed
#: run can be repaired. A test that wrote it would make the next run "repair" a
#: file nobody mutated, and overwrite real work.
RECOVERY = ROOT / "scripts/.mutate-recovery.json"


def _state(path):
    return path.read_bytes() if path.exists() else None


@pytest.fixture(autouse=True, scope="session")
def _the_real_recovery_file_is_never_touched():
    """No test may create, change or remove the mutation check's recovery file.

    It is compared, not required absent: during a mutation run the file exists,
    written just before the suite starts, and must come through it unchanged.
    """
    before = _state(RECOVERY)
    yield
    after = _state(RECOVERY)
    assert after == before, (
        f"a test touched {RECOVERY.relative_to(ROOT)}; point the runner's RECOVERY at tmp_path first"
    )
