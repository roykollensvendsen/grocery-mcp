"""The status file the voice bridge reads, in the shape every plugin shares."""

import datetime as dt
import json
import os
import pathlib
import re

import pytest

from kassalapp_mcp.fake import FAKE_KEY, FakeKassalapp
from kassalapp_mcp.quota import Quota
from kassalapp_mcp.source import KassalappSource, Product, SlowDownError

OSLO = dt.timezone(dt.timedelta(hours=1))
LETT = Product("7000000000017", "Fjellmelk lett 1 l")
AT = dt.datetime(2026, 11, 14, 12, 0, 10, tzinfo=OSLO)
#: The bridge's limits on what it shows, from the shared status design.
SAY_MAX, FIGURES_MAX, FIGURE_NAME = 120, 8, re.compile(r"[a-z_]{1,32}")


@pytest.fixture
def fake():
    return FakeKassalapp(today=AT.date())


def _status(fake, tmp_path, *, calls=1):
    quota = Quota(tmp_path, now=lambda: AT)
    source = KassalappSource(fake.client(), lambda: FAKE_KEY, quota=quota)
    for _ in range(calls):
        source.prices([LETT])
    return json.loads((tmp_path / "status.json").read_text())


def test_the_status_file_has_the_shape_every_plugin_shares(fake, tmp_path):
    status = _status(fake, tmp_path)
    assert set(status) == {"at", "ok", "say", "figures"}
    assert status["at"] == AT.isoformat()
    assert isinstance(status["ok"], bool)
    assert len(status["say"]) <= SAY_MAX
    assert len(status["figures"]) <= FIGURES_MAX
    for name, value in status["figures"].items():
        assert FIGURE_NAME.fullmatch(name), name
        assert isinstance(value, int | float), name


def test_the_status_says_how_many_calls_are_left(fake, tmp_path):
    status = _status(fake, tmp_path)
    assert status["ok"] is True
    assert status["say"] == "59 of 60 calls left this minute"
    figures = status["figures"]
    assert (figures["calls_left"], figures["calls_per_minute"]) == (59, 60)
    assert (figures["calls_last_minute"], figures["calls_today"]) == (1, 1)


def test_a_missing_header_is_left_out_of_the_figures(fake, tmp_path):
    fake.remaining = None
    status = _status(fake, tmp_path)
    assert "calls_left" not in status["figures"]
    assert "calls_per_minute" not in status["figures"]
    assert status["say"] == "1 call today; Kassalapp has not said how many are left"


def test_a_new_rate_limit_header_becomes_a_figure_of_its_own(fake, tmp_path):
    fake.extra_headers = {"X-RateLimit-Day": "5000"}
    assert _status(fake, tmp_path)["figures"]["ratelimit_day"] == 5000


def test_few_calls_left_is_not_ok(fake, tmp_path):
    fake.remaining = 11
    status = _status(fake, tmp_path)
    assert status["ok"] is False
    assert status["say"] == "Low: 10 of 60 calls left this minute"


def test_backing_off_is_not_ok_and_says_how_long(fake, tmp_path):
    quota = Quota(tmp_path, now=lambda: AT)
    fake.retry_after = 30
    with pytest.raises(SlowDownError):
        KassalappSource(fake.client(), lambda: FAKE_KEY, quota=quota).prices([LETT])
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["ok"] is False
    assert status["say"] == "Waiting 30 s, as Kassalapp asked"
    assert status["figures"]["waiting_seconds"] == 30
    assert status["figures"]["too_many_today"] == 1


def test_the_status_file_is_replaced_whole_never_written_in_place(fake, tmp_path, monkeypatch):
    replaced = []
    real = os.replace

    def spy(source, target):
        replaced.append(pathlib.Path(target).name)
        real(source, target)

    monkeypatch.setattr(os, "replace", spy)
    _status(fake, tmp_path)
    assert "status.json" in replaced
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".")) == []


def test_the_count_survives_a_restart_for_the_quota_command(fake, tmp_path):
    _status(fake, tmp_path)
    again = Quota(tmp_path, now=lambda: AT).status()
    assert (again["remaining"], again["calls_today"]) == (59, 1)
