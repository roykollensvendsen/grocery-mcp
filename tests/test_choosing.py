"""Choosing one product when the words fit several, as the real fuzzy search makes common."""

import datetime as dt

import pytest

from grocery_mcp.answers import PriceAnswers
from grocery_mcp.cache import CachedSource
from grocery_mcp.fake import FAKE_KEY, FakeKassalapp
from grocery_mcp.source import KassalappSource

TODAY = dt.date(2026, 11, 14)
NOON = dt.datetime(2026, 11, 14, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=1)))


@pytest.fixture
def fake():
    return FakeKassalapp(today=TODAY)


def _answers(fake, *, cached=False):
    source = KassalappSource(fake.client(), lambda: FAKE_KEY)
    if cached:
        source = CachedSource(source, ":memory:", now=lambda: NOON)
    return PriceAnswers(source, today=lambda: TODAY)


def _searches(fake):
    return sum(r.url.path.endswith("/products") for r in fake.requests)


def test_the_fake_search_is_fuzzy_like_the_real_one(fake):
    found = KassalappSource(fake.client(), lambda: FAKE_KEY).find("fjellmelk lett 1 l")
    assert len(found) > 1
    assert found[0].name == "Fjellmelk lett 1 l"


def test_each_choice_carries_its_barcode(fake):
    answer = _answers(fake).grocery_price("fjellmelk")
    assert answer["choices"][0] == {"name": "Fjellmelk lett 1 l", "ean": "7000000000017"}


def test_answering_an_offered_choice_gives_a_price_never_the_question_again(fake):
    answers = _answers(fake)
    asked = answers.grocery_price("fjellmelk", chain="Meny")
    answer = answers.grocery_price(asked["choices"][1]["name"], chain="Meny")
    assert answer["outcome"] == "price"
    assert answer["say"].startswith("Fjellmelk hel 1 l costs 27.90 at Meny")


def test_a_barcode_from_the_choices_gives_that_products_price(fake):
    answers = _answers(fake)
    asked = answers.grocery_price("fjellmelk")
    answer = answers.grocery_price(asked["choices"][2]["ean"], chain="Meny")
    assert answer["say"].startswith("Fjellmelk skummet 1 l costs 25.90 at Meny")
    assert _searches(fake) == 1


def test_a_barcode_kassalapp_does_not_know_is_said_plainly(fake):
    answer = _answers(fake).grocery_price("7000000009999")
    assert answer["outcome"] == "not_found"


def test_the_choice_is_kept_under_the_first_words(fake):
    answers = _answers(fake, cached=True)
    asked = answers.grocery_price("fjellmelk")
    answers.grocery_price(asked["choices"][0]["name"])
    searches = _searches(fake)
    again = answers.grocery_price("fjellmelk", chain="Meny")
    assert again["say"].startswith("Fjellmelk lett 1 l costs 26.90 at Meny")
    assert _searches(fake) == searches


def test_the_full_name_alone_is_chosen_without_asking(fake):
    answer = _answers(fake).grocery_cheapest("Fjellmelk  lett 1 liter")
    assert answer["outcome"] == "cheapest"
