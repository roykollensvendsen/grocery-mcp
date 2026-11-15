"""The cache in front of a price source, so the same question never costs twice."""

import datetime as dt

import httpx
import pytest

from grocery_mcp.answers import PriceAnswers
from grocery_mcp.cache import CachedSource, kept_until, normalised
from grocery_mcp.fake import FAKE_KEY, FakeKassalapp
from grocery_mcp.source import Product
from grocery_mcp.sources.kassalapp import KassalappSource

OSLO = dt.timezone(dt.timedelta(hours=1))
LETT = Product("7000000000017", "Fjellmelk lett 1 l")


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


@pytest.fixture
def fake():
    return FakeKassalapp(today=dt.date(2026, 11, 14))


def _cached(fake, clock, path=":memory:"):
    return CachedSource(KassalappSource(fake.client(), lambda: FAKE_KEY), path, now=clock)


def _calls(fake, ending):
    return sum(r.url.path.endswith(ending) for r in fake.requests)


def test_the_same_question_twice_in_six_hours_costs_no_second_call(fake):
    clock = Clock(dt.datetime(2026, 11, 14, 12, 0, tzinfo=OSLO))
    answers = PriceAnswers(_cached(fake, clock), today=lambda: clock().date())
    first = answers.grocery_price("fjellmelk lett 1 l", chain="Meny")
    calls = len(fake.requests)
    clock.at += dt.timedelta(hours=5, minutes=59)
    second = answers.grocery_price("fjellmelk lett 1 l", chain="Meny")
    assert len(fake.requests) == calls
    assert second["say"].startswith(first["say"].split(" Kassalapp's")[0])


def test_a_cached_price_is_used_for_six_hours(fake):
    clock = Clock(dt.datetime(2026, 11, 14, 12, 0, tzinfo=OSLO))
    source = _cached(fake, clock)
    source.prices([LETT])
    clock.at += dt.timedelta(hours=5)
    source.prices([LETT])
    assert _calls(fake, "prices-bulk") == 1
    clock.at += dt.timedelta(hours=1, minutes=1)
    source.prices([LETT])
    assert _calls(fake, "prices-bulk") == 2


def test_a_price_fetched_before_nine_is_held_only_until_ten(fake):
    clock = Clock(dt.datetime(2026, 11, 14, 7, 0, tzinfo=OSLO))
    source = _cached(fake, clock)
    source.prices([LETT])
    clock.at = dt.datetime(2026, 11, 14, 9, 59, tzinfo=OSLO)
    source.prices([LETT])
    assert _calls(fake, "prices-bulk") == 1
    clock.at = dt.datetime(2026, 11, 14, 10, 1, tzinfo=OSLO)
    source.prices([LETT])
    assert _calls(fake, "prices-bulk") == 2


def test_a_price_fetched_in_the_night_is_held_until_ten(fake):
    clock = Clock(dt.datetime(2026, 11, 14, 2, 0, tzinfo=OSLO))
    source = _cached(fake, clock)
    source.prices([LETT])
    clock.at = dt.datetime(2026, 11, 14, 9, 30, tzinfo=OSLO)
    source.prices([LETT])
    assert _calls(fake, "prices-bulk") == 1


def test_words_that_found_one_product_are_kept_while_in_use(fake):
    clock = Clock(dt.datetime(2026, 11, 14, 12, 0, tzinfo=OSLO))
    source = _cached(fake, clock)
    coffee = Product("7000000000055", "Bergkaffe filtermalt 500 g")
    assert source.find("Bergkaffe") == [coffee]
    clock.at += dt.timedelta(days=89)
    assert source.find("  bergkaffe ") == [coffee]
    assert _calls(fake, "/products") == 1


def test_words_that_fit_several_products_are_searched_again(fake):
    source = _cached(fake, Clock(dt.datetime(2026, 11, 14, 12, 0, tzinfo=OSLO)))
    source.find("fjellmelk")
    source.find("fjellmelk")
    assert _calls(fake, "/products") == 2


def test_only_the_products_not_cached_are_fetched(fake):
    source = _cached(fake, Clock(dt.datetime(2026, 11, 14, 12, 0, tzinfo=OSLO)))
    source.prices([LETT])
    coffee = Product("7000000000055", "Bergkaffe filtermalt 500 g")
    prices = source.prices([LETT, coffee])
    assert {p.chain for p in prices if p.product == coffee} == {"JOKER_NO"}
    assert fake.requests[-1].content.count(b"7000000000055") == 1
    assert b"7000000000017" not in fake.requests[-1].content


def test_the_cache_survives_a_restart(fake, tmp_path):
    clock = Clock(dt.datetime(2026, 11, 14, 12, 0, tzinfo=OSLO))
    path = tmp_path / "cache.sqlite"
    _cached(fake, clock, path).prices([LETT])
    again = _cached(fake, clock, path).prices([LETT])
    assert _calls(fake, "prices-bulk") == 1
    meny = next(p for p in again if p.chain == "MENY_NO")
    assert (str(meny.price), meny.seen_at) == ("26.90", dt.date(2026, 11, 14))


def test_spoken_words_are_normalised():
    assert normalised("  Tine   LETTMELK 1 liter ") == "tine lettmelk 1l"
    assert normalised("kaffe 500 gram") == "kaffe 500g"


def test_the_morning_rule_follows_norwegian_time_on_a_utc_machine():
    utc = dt.UTC
    # 07:30 UTC in November is 08:30 in Oslo: before the update, so kept until 10:00 Oslo.
    november = kept_until(dt.datetime(2026, 11, 14, 7, 30, tzinfo=utc))
    assert november == dt.datetime(2026, 11, 14, 9, 0, tzinfo=utc)
    # 08:30 UTC in November is 09:30 in Oslo: after the start, so six hours.
    assert kept_until(dt.datetime(2026, 11, 14, 8, 30, tzinfo=utc)) == dt.datetime(
        2026, 11, 14, 14, 30, tzinfo=utc
    )


def test_the_morning_rule_follows_summer_time():
    utc = dt.UTC
    # 06:30 UTC in July is 08:30 in Oslo, two hours ahead in summer.
    assert kept_until(dt.datetime(2026, 7, 1, 6, 30, tzinfo=utc)) == dt.datetime(2026, 7, 1, 8, 0, tzinfo=utc)
    # 07:30 UTC in July is 09:30 in Oslo: six hours.
    assert kept_until(dt.datetime(2026, 7, 1, 7, 30, tzinfo=utc)) == dt.datetime(
        2026, 7, 1, 13, 30, tzinfo=utc
    )


def test_remembered_words_are_forgotten_after_ninety_days_unused(fake):
    clock = Clock(dt.datetime(2026, 11, 14, 12, 0, tzinfo=OSLO))
    source = _cached(fake, clock)
    source.find("bergkaffe")
    clock.at += dt.timedelta(days=60)
    source.find("bergkaffe")
    clock.at += dt.timedelta(days=60)
    source.find("bergkaffe")
    assert _calls(fake, "/products") == 1
    clock.at += dt.timedelta(days=91)
    source.find("bergkaffe")
    assert _calls(fake, "/products") == 2


def test_an_old_price_answers_when_kassalapp_cannot_be_reached(fake, tmp_path):
    clock = Clock(dt.datetime(2026, 11, 14, 12, 0, tzinfo=OSLO))
    path = tmp_path / "cache.sqlite"
    first = _cached(fake, clock, path).prices([LETT])
    clock.at += dt.timedelta(hours=7)

    def unreachable(request):
        message = "no route"
        raise httpx.ConnectError(message, request=request)

    client = httpx.Client(base_url=fake.client().base_url, transport=httpx.MockTransport(unreachable))
    cut_off = CachedSource(KassalappSource(client, lambda: FAKE_KEY), path, now=clock)
    assert cut_off.prices([LETT]) == first
