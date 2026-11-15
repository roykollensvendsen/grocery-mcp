"""A name in a spoken sentence is one short line, whoever wrote it."""

import datetime as dt

from kassalapp_mcp.answers import SPOKEN_MAX, PriceAnswers, spoken
from kassalapp_mcp.fake import FAKE_KEY, FakeKassalapp, FakeProduct
from kassalapp_mcp.source import KassalappSource

TODAY = dt.date(2026, 11, 14)
LONG = "Fjellmelk ekstra\nlett\t1 l " + "med en svært lang beskrivelse " * 6 + "\x07slutt"


def _answers(fake):
    return PriceAnswers(KassalappSource(fake.client(), lambda: FAKE_KEY), today=lambda: TODAY)


def test_a_name_in_a_spoken_sentence_is_one_short_line():
    said = spoken(LONG)
    assert "\n" not in said
    assert "\t" not in said
    assert "\x07" not in said
    assert len(said) <= SPOKEN_MAX
    assert said.startswith("Fjellmelk ekstra lett 1 l med")


def test_a_long_product_name_from_kassalapp_is_cut_in_the_answer():
    fake = FakeKassalapp(today=TODAY)
    fake.products = [FakeProduct("7000000000086", LONG, {"MENY_NO": ("Meny\nSentrum", "9.90", 0)})]
    say = _answers(fake).grocery_price("7000000000086")["say"]
    assert "\n" not in say
    assert "beskrivelse " * 3 not in say


def test_long_spoken_words_are_cut_in_the_answer():
    fake = FakeKassalapp(today=TODAY)
    words = "surströmming " * 20
    say = _answers(fake).grocery_price(words)["say"]
    assert len(say) < len("Kassalapp has no product called .") + SPOKEN_MAX + 1
    say = _answers(fake).grocery_price("bergkaffe", chain="Rema\n" + "x" * 200)["say"]
    assert "\n" not in say
    assert "x" * (SPOKEN_MAX + 1) not in say
