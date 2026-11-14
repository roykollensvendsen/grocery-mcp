"""Counting the calls to Kassalapp, and keeping within its free limit."""

import datetime as dt
import json
import logging

import pytest

from kassalapp_mcp.answers import SLOW_DOWN, PriceAnswers
from kassalapp_mcp.cache import CachedSource
from kassalapp_mcp.cli import main
from kassalapp_mcp.fake import FAKE_KEY, FakeKassalapp
from kassalapp_mcp.quota import Quota
from kassalapp_mcp.source import KassalappSource, Product, SlowDownError

OSLO = dt.timezone(dt.timedelta(hours=1))
LETT = Product("7000000000017", "Fjellmelk lett 1 l")


class Clock:
    def __init__(self):
        self.at = dt.datetime(2026, 11, 14, 12, 0, 10, tzinfo=OSLO)

    def __call__(self):
        return self.at


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fake():
    return FakeKassalapp(today=dt.date(2026, 11, 14))


@pytest.fixture
def quota(tmp_path, clock):
    return Quota(tmp_path, now=clock)


def _source(fake, quota):
    return KassalappSource(fake.client(), lambda: FAKE_KEY, quota=quota)


def test_the_headers_are_read_and_shown(fake, quota, clock):
    _source(fake, quota).prices([LETT])
    status = quota.status()
    assert (status["limit_per_min"], status["remaining"]) == (60, 59)
    assert status["seen_at"] == clock().isoformat()


def test_a_missing_header_stays_missing(fake, quota):
    fake.remaining = None
    _source(fake, quota).prices([LETT])
    status = quota.status()
    assert status["remaining"] is None
    assert status["limit_per_min"] is None


def test_a_new_rate_limit_header_shows_in_the_status_unchanged(fake, quota):
    fake.extra_headers = {"X-RateLimit-Day": "5000"}
    _source(fake, quota).prices([LETT])
    assert quota.status()["headers"]["x-ratelimit-day"] == "5000"


def test_the_level_warns_at_twelve_remaining(fake, quota):
    fake.remaining = 14
    source = _source(fake, quota)
    source.prices([LETT])
    assert quota.status()["level"] == "ok"
    source.prices([LETT])
    assert quota.status()["level"] == "warn"


def test_the_level_warns_at_forty_eight_of_our_own_calls_a_minute(fake, quota, clock):
    fake.remaining = None
    source = _source(fake, quota)
    for _ in range(47):
        source.prices([LETT])
    assert quota.status()["level"] == "ok"
    source.prices([LETT])
    assert quota.status()["level"] == "warn"
    clock.at += dt.timedelta(seconds=61)
    assert quota.status()["level"] == "ok"


def test_a_warning_is_logged_at_most_once_a_minute(fake, quota, clock, caplog):
    fake.remaining = 10
    source = _source(fake, quota)
    with caplog.at_level(logging.WARNING, logger="kassalapp_mcp.quota"):
        source.prices([LETT])
        source.prices([LETT])
        clock.at += dt.timedelta(seconds=61)
        source.prices([LETT])
    assert len(caplog.records) == 2


def test_a_429_stops_calls_until_retry_after_passes(fake, quota, clock):
    source = _source(fake, quota)
    fake.retry_after = 30
    with pytest.raises(SlowDownError):
        source.prices([LETT])
    fake.retry_after = None
    sent = len(fake.requests)
    clock.at += dt.timedelta(seconds=29)
    with pytest.raises(SlowDownError):
        source.prices([LETT])
    assert len(fake.requests) == sent
    assert quota.status()["level"] == "backing_off"
    clock.at += dt.timedelta(seconds=2)
    source.prices([LETT])
    assert len(fake.requests) == sent + 1


def test_a_429_without_retry_after_waits_for_the_minute_to_turn(fake, quota):
    source = _source(fake, quota)
    fake.retry_after = 0
    fake.retry_after_header = False
    with pytest.raises(SlowDownError):
        source.prices([LETT])
    fake.retry_after = None
    assert quota.status()["backing_off_until"] == "2026-11-14T12:01:00+01:00"


def test_nothing_is_left_no_call_is_made_until_the_minute_turns(fake, quota, clock):
    fake.remaining = 1
    source = _source(fake, quota)
    source.prices([LETT])
    sent = len(fake.requests)
    with pytest.raises(SlowDownError):
        source.prices([LETT])
    assert len(fake.requests) == sent
    clock.at = dt.datetime(2026, 11, 14, 12, 1, 0, tzinfo=OSLO)
    source.prices([LETT])
    assert len(fake.requests) == sent + 1


def test_sixty_of_our_own_calls_in_a_minute_stop_the_next(fake, quota):
    fake.remaining = None
    source = _source(fake, quota)
    for _ in range(60):
        source.prices([LETT])
    with pytest.raises(SlowDownError):
        source.prices([LETT])
    assert len(fake.requests) == 60


def test_while_backing_off_the_cache_answers_with_its_age(fake, quota, clock):
    cached = CachedSource(_source(fake, quota), ":memory:", now=clock, on_hit=quota.cache_hit)
    answers = PriceAnswers(cached, today=lambda: clock().date())
    answers.grocery_price("fjellmelk lett", chain="Meny")
    clock.at += dt.timedelta(hours=7)
    fake.retry_after = 30
    answer = answers.grocery_price("fjellmelk lett", chain="Meny")
    assert answer["say"].startswith("Fjellmelk lett 1 l costs 26.90 at Meny, checked today.")


def test_with_nothing_cached_backing_off_is_one_sentence(fake, quota, clock):
    cached = CachedSource(_source(fake, quota), ":memory:", now=clock, on_hit=quota.cache_hit)
    fake.retry_after = 30
    answer = PriceAnswers(cached, today=lambda: clock().date()).grocery_price("fjellmelk lett")
    assert answer["say"] == SLOW_DOWN


def test_each_call_is_one_line_in_the_usage_log(fake, quota, tmp_path, clock):
    source = _source(fake, quota)
    source.find("fjellmelk lett")
    source.prices([LETT])
    lines = [json.loads(x) for x in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert [x["endpoint"] for x in lines] == ["/products", "/products/prices-bulk"]
    assert lines[0] == {
        "at": clock().isoformat(),
        "endpoint": "/products",
        "status": 200,
        "remaining": 59,
        "from_cache": False,
    }


def test_the_usage_log_never_holds_a_search_word_or_barcode(fake, quota, tmp_path):
    source = _source(fake, quota)
    source.find("fjellmelk lett")
    source.prices([LETT])
    fake.retry_after = 5
    with pytest.raises(SlowDownError):
        source.find("bergkaffe")
    for name in ("usage.jsonl", "usage-days.json", "status.json"):
        text = (tmp_path / name).read_text().lower()
        for secret in ("fjellmelk", "bergkaffe", "7000000000017"):
            assert secret not in text, f"{secret} in {name}"


def test_day_totals_count_calls_cache_hits_and_429s(fake, quota, tmp_path, clock):
    cached = CachedSource(_source(fake, quota), ":memory:", now=clock, on_hit=quota.cache_hit)
    cached.find("fjellmelk lett")
    cached.find("fjellmelk lett")
    cached.prices([LETT])
    cached.prices([LETT])
    fake.retry_after = 5
    clock.at += dt.timedelta(hours=7)
    cached.prices([LETT])
    days = json.loads((tmp_path / "usage-days.json").read_text())
    assert days["2026-11-14"] == {"calls": 3, "cache_hits": 3, "429s": 1}
    status = quota.status()
    assert (status["calls_today"], status["cache_hits_today"], status["429s_today"]) == (3, 3, 1)


def test_day_totals_are_kept_ninety_days(fake, tmp_path, clock):
    old = {"2026-08-15": {"calls": 1, "cache_hits": 0, "429s": 0}, "2026-08-16": {"calls": 2}}
    (tmp_path / "usage-days.json").write_text(json.dumps(old))
    quota = Quota(tmp_path, now=clock)
    _source(fake, quota).prices([LETT])
    days = json.loads((tmp_path / "usage-days.json").read_text())
    assert "2026-08-15" not in days
    assert "2026-08-16" in days


def test_the_usage_log_is_cut_to_its_newest_lines(fake, tmp_path, clock):
    quota = Quota(tmp_path, now=clock, keep_lines=10)
    fake.remaining = None
    source = _source(fake, quota)
    for _ in range(25):
        source.prices([LETT])
    assert len((tmp_path / "usage.jsonl").read_text().splitlines()) <= 10


def test_the_quota_command_prints_the_status_from_its_folder(tmp_path, capsys):
    assert main(["quota", "--state-dir", str(tmp_path)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["level"] == "ok"
    assert status["calls_today"] == 0
