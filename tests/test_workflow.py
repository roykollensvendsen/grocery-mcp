"""What the CI workflow promises about its own runs."""

import pathlib
import re

ROOT = pathlib.Path(__file__).parent.parent
WORKFLOW = ROOT / ".github/workflows/checks.yml"


def _concurrency():
    text = WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"^concurrency:\n((?:  .*\n)+)", text, re.MULTILINE)
    assert block, "the workflow has no concurrency block"
    return block[1]


def test_a_new_push_cancels_only_a_pull_request_s_older_run():
    """A pull request's superseded run is wasted runner time; a run on main keeps main's levels.

    The quality job measures and keeps main's levels on a push to main, so a run there
    must always finish, even when another push follows it.
    """
    assert "  cancel-in-progress: ${{ github.event_name == 'pull_request' }}\n" in _concurrency()


def test_every_run_not_for_a_pull_request_is_a_group_of_its_own():
    """GitHub keeps one waiting run per group, so one group for main skipped a commit between two.

    Keyed by its own run id, every run on main, a tag or by hand waits its turn and runs.
    """
    group = "${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.ref || github.run_id }}"
    assert f"  group: {group}\n" in _concurrency()
