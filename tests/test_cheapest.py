"""Where a product is cheapest, against the fake service."""

import datetime as dt

import pytest

from grocery_mcp.answers import NOT_SET_UP, PriceAnswers
from grocery_mcp.fake import FAKE_KEY, FakeKassalapp, FakeProduct
from grocery_mcp.source import KassalappSource

TODAY = dt.date(2026, 11, 14)


@pytest.fixture
def fake():
    return FakeKassalapp(today=TODAY)


def _answers(fake, key=FAKE_KEY):
    return PriceAnswers(KassalappSource(fake.client(), lambda: key), today=lambda: TODAY)


def test_the_cheapest_chain_comes_first_and_the_next_after_it(fake):
    answer = _answers(fake).grocery_cheapest("fjellmelk lett 1 l")
    assert answer["outcome"] == "cheapest"
    assert answer["say"].startswith(
        "Fjellmelk lett 1 l is cheapest at Joker, 24.90, checked yesterday."
        " Then Meny at 26.90, checked today. No current prices from Kiwi, REMA or Coop."
    )
    assert [p["chain"] for p in answer["prices"]] == ["Joker", "Meny"]


def test_a_stale_price_is_never_the_cheapest(fake):
    # Kiwi's 21.90 and Spar's 25.50 are both older than fourteen days.
    say = _answers(fake).grocery_cheapest("fjellmelk lett 1 l")["say"]
    assert "21.90" not in say
    assert "Spar" not in say


def test_one_chain_alone_is_said_to_be_the_only_one(fake):
    say = _answers(fake).grocery_cheapest("bergkaffe")["say"]
    assert say.startswith(
        "Only Joker has a current price for Bergkaffe filtermalt 500 g: 79.90, checked today."
    )


def test_chains_at_the_same_price_are_named_together(fake):
    fake.products.append(
        FakeProduct(
            "7000000000062",
            "Fjordsalt grovt 1 kg",
            {"MENY_NO": ("Meny", "19.90", 0), "JOKER_NO": ("Joker", "19.90", 0)},
        )
    )
    say = _answers(fake).grocery_cheapest("fjordsalt")["say"]
    assert say.startswith("Fjordsalt grovt 1 kg costs the same, 19.90, at Joker and Meny, checked today.")


def test_several_matches_for_cheapest_give_a_question_too(fake):
    answer = _answers(fake).grocery_cheapest("fjellmelk")
    assert answer["outcome"] == "ask"
    # No price is said before a choice. One bulk call ranks every match, most sold first.
    assert "costs" not in answer["say"]
    assert sum(r.url.path.endswith("prices-bulk") for r in fake.requests) == 1


def test_no_current_price_anywhere_is_said_plainly(fake):
    fake.products.append(FakeProduct("7000000000079", "Gammelost 300 g", {"KIWI": ("Kiwi", "89.90", 400)}))
    answer = _answers(fake).grocery_cheapest("gammelost")
    assert answer["outcome"] == "no_current_price"
    assert answer["say"].startswith("Kassalapp has no current price for Gammelost 300 g at any chain.")


def test_cheapest_without_a_key_sends_nothing(fake):
    answer = _answers(fake, key="").grocery_cheapest("fjellmelk lett 1 l")
    assert answer["say"] == NOT_SET_UP
    assert fake.requests == []
