"""What the voice hears when asked for a price, against the fake service."""

import datetime as dt

import pytest

from kassalapp_mcp.answers import PriceAnswers
from kassalapp_mcp.fake import FAKE_KEY, FakeKassalapp
from kassalapp_mcp.source import KassalappSource

TODAY = dt.date(2026, 11, 14)  # a Saturday


@pytest.fixture
def fake():
    return FakeKassalapp(today=TODAY)


@pytest.fixture
def answers(fake, tmp_path):
    key = tmp_path / "key"
    key.write_text(FAKE_KEY)
    return PriceAnswers(KassalappSource(fake.client(), key), today=lambda: TODAY)


def test_one_match_gives_the_price_and_how_old_it_is(answers):
    answer = answers.grocery_price("fjellmelk lett", chain="Meny")
    assert answer["outcome"] == "price"
    assert answer["say"].startswith("Fjellmelk lett 1 l costs 26.90 at Meny, checked today.")


def test_the_first_answer_says_prices_are_per_chain_and_later_ones_do_not(answers):
    first = answers.grocery_price("fjellmelk lett", chain="Meny")
    second = answers.grocery_price("fjellmelk lett", chain="Meny")
    assert "per chain, not per shop" in first["say"]
    assert "per chain" not in second["say"]


def test_without_a_chain_every_current_price_is_given_cheapest_first(answers):
    say = answers.grocery_price("fjellmelk lett")["say"]
    assert "24.90 at Joker, checked yesterday" in say
    assert say.index("Joker") < say.index("Meny")


def test_several_matches_give_a_question_never_a_guess(answers, fake):
    answer = answers.grocery_price("fjellmelk")
    assert answer["outcome"] == "ask"
    assert answer["say"].startswith("Which one:")
    assert len(answer["choices"]) == 3
    assert not any(r.url.path.endswith("prices-bulk") for r in fake.requests)


def test_kiwi_is_said_to_have_no_current_price(answers):
    answer = answers.grocery_price("fjellmelk lett", chain="Kiwi")
    assert answer["outcome"] == "no_current_price"
    assert "Kassalapp has no current prices from Kiwi; the last one is from March 2025." in answer["say"]


def test_a_price_older_than_fourteen_days_is_never_called_current(answers):
    one = answers.grocery_price("fjellmelk lett", chain="Spar")
    assert one["outcome"] == "no_current_price"
    assert "the last known price, from October" in one["say"]
    every = answers.grocery_price("fjellmelk lett")["say"]
    assert "Spar" not in every
    assert "Kiwi" not in every.split("No current")[0]


def test_a_product_kassalapp_does_not_have_is_said_plainly(answers):
    answer = answers.grocery_price("surströmming")
    assert answer["outcome"] == "not_found"
    assert "surströmming" in answer["say"]


def test_a_chain_without_the_product_is_named(answers):
    answer = answers.grocery_price("bergkaffe", chain="Meny")
    assert answer["outcome"] == "no_current_price"
    assert "Meny" in answer["say"]


def test_without_a_key_the_voice_is_told_to_set_one_up(fake, tmp_path):
    answers = PriceAnswers(KassalappSource(fake.client(), tmp_path / "none"), today=lambda: TODAY)
    answer = answers.grocery_price("fjellmelk lett")
    assert answer["outcome"] == "not_set_up"
    assert answer["say"] == "Kassalapp isn't set up; add your own key, see the README."
    assert fake.requests == []


def test_a_request_to_slow_down_is_one_sentence(answers, fake):
    fake.retry_after = 30
    answer = answers.grocery_price("fjellmelk lett")
    assert answer["say"] == "Kassalapp has asked us to slow down; ask again in a minute."


def test_a_refused_key_says_where_to_make_a_new_one(fake, tmp_path):
    key = tmp_path / "key"
    key.write_text("wrong")
    answer = PriceAnswers(KassalappSource(fake.client(), key), today=lambda: TODAY).grocery_price("fjellmelk")
    assert answer["outcome"] == "refused"
    assert "kassal.app/profil/api" in answer["say"]


def test_two_letters_are_too_few_to_search(answers):
    assert answers.grocery_price("ku")["say"] == "Say at least three letters of the product."


def test_every_answer_has_something_to_say_and_an_outcome(answers):
    for words in ("fjellmelk lett", "fjellmelk", "surströmming", "ku"):
        answer = answers.grocery_price(words)
        assert answer["say"]
        assert answer["outcome"]
        assert "success" in answer
