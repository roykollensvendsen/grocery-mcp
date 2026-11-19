"""A bare word like "lettmelk" offers the everyday product first, not a niche line.

The owner asked by voice for "lettmelk" and heard an organic own-brand milk first, sold
in one chain, ahead of the milk every chain sells. Kassalapp's search order says
how well the words match, not how common the product is; the bulk price answer
says how many chains sell it now.
"""

import datetime as dt

import httpx
import pytest

from grocery_mcp.answers import PriceAnswers
from grocery_mcp.fake import FAKE_KEY, FakeKassalapp, FakeProduct
from grocery_mcp.source import Product, SlowDownError
from grocery_mcp.sources.kassalapp import KassalappSource

TODAY = dt.date(2026, 11, 15)
EVERYWHERE = {
    "MENY_NO": ("Meny", "21.70", 0),
    "JOKER_NO": ("Joker", "22.90", 0),
    "SPAR_NO": ("Spar", "21.90", 1),
    "ODA": ("Oda", "19.20", 0),
}


@pytest.fixture
def fake():
    """Shaped like the live case: the niche lines match the word first, the common milk last."""
    fake = FakeKassalapp(today=TODAY)
    fake.products = [
        FakeProduct("7000000000101", "Grønnlund Lettmelk økologisk 1 l", {"MENY_NO": ("Meny", "27.90", 0)}),
        FakeProduct("7000000000118", "Fjordgård Lettmelk 1 l", {"SPAR_NO": ("Spar", "20.90", 0)}),
        FakeProduct(
            "7000000000125",
            "Dalsbygd Lettmelk laktosefri 1 l",
            {"MENY_NO": ("Meny", "29.90", 0), "ODA": ("Oda", "28.50", 0)},
        ),
        FakeProduct("7000000000132", "Fjellmeieri Lettmelk 0,5 % 1 l", EVERYWHERE),
    ]
    return fake


def _answers(fake):
    return PriceAnswers(KassalappSource(fake.client(), lambda: FAKE_KEY), today=lambda: TODAY)


def test_the_choices_are_offered_most_sold_first(fake):
    answer = _answers(fake).grocery_price("lettmelk")
    assert answer["outcome"] == "ask"
    assert [c["name"] for c in answer["choices"]] == [
        "Fjellmeieri Lettmelk 0,5 % 1 l",
        "Dalsbygd Lettmelk laktosefri 1 l",
        "Grønnlund Lettmelk økologisk 1 l",
    ]
    assert answer["say"].startswith("Which one: Fjellmeieri Lettmelk 0,5 % 1 l,")


def test_a_price_older_than_allowed_does_not_count_as_sold(fake):
    """Kassalapp still lists chains it stopped reading; those do not make a product common."""
    fake.products[0].prices.update({f"OLD{n}": (f"Old {n}", "9.90", 600) for n in range(5)})
    answer = _answers(fake).grocery_price("lettmelk")
    assert answer["choices"][0]["name"] == "Fjellmeieri Lettmelk 0,5 % 1 l"


def test_equally_common_products_keep_kassalapp_s_order(fake):
    fake.products = [
        FakeProduct("7000000000101", "Grønnlund Lettmelk 1 l", {"MENY_NO": ("Meny", "27.90", 0)}),
        FakeProduct("7000000000118", "Fjordgård Lettmelk 1 l", {"SPAR_NO": ("Spar", "20.90", 0)}),
    ]
    answer = _answers(fake).grocery_price("lettmelk")
    assert [c["name"] for c in answer["choices"]] == ["Grønnlund Lettmelk 1 l", "Fjordgård Lettmelk 1 l"]


def test_the_chosen_product_s_price_needs_no_second_call_when_cached(fake):
    """Ranking fetches every choice's prices once; with the cache, choosing one sends nothing more."""
    from grocery_mcp.cache import CachedSource  # noqa: PLC0415

    noon = dt.datetime(2026, 11, 15, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=1)))
    source = CachedSource(KassalappSource(fake.client(), lambda: FAKE_KEY), ":memory:", now=lambda: noon)
    answers = PriceAnswers(source, today=lambda: TODAY)
    asked = answers.grocery_price("lettmelk")
    sent = len(fake.requests)
    answer = answers.grocery_price(asked["choices"][0]["ean"])
    assert answer["outcome"] == "price"
    assert len(fake.requests) == sent


class _PricesRefused:
    """A source whose search works but whose price call is turned away."""

    name = "kassalapp"

    def __init__(self, inner, error):
        self._inner, self._error = inner, error

    def find(self, words):
        return self._inner.find(words)

    def prices(self, _products):
        raise self._error

    def remember(self, words, product):
        pass


@pytest.mark.parametrize(
    "error",
    [
        SlowDownError(30),
        httpx.ConnectError("down"),
        httpx.HTTPStatusError("503", request=None, response=None),
    ],
    ids=["slow down", "not reachable", "server error"],
)
def test_a_failed_ranking_still_asks_the_question(fake, error):
    source = _PricesRefused(KassalappSource(fake.client(), lambda: FAKE_KEY), error)
    answer = PriceAnswers(source, today=lambda: TODAY).grocery_price("lettmelk")
    assert answer["outcome"] == "ask"
    assert answer["choices"][0]["name"] == "Grønnlund Lettmelk økologisk 1 l"


def test_a_duplicate_with_no_current_price_is_offered_after_the_one_with_prices(fake):
    """Kassalapp lists some products twice; the copy no chain has priced lately is a dead end."""
    fake.products = [
        FakeProduct("7000000000149", "Kakaogård Sjokoladedrikk Fjellbris", {}),
        FakeProduct(
            "7000000000156",
            "Sjokoladedrikk 450g boks Kakaogård",
            {
                "ODA": ("Oda", "84.70", 0),
                "MENY_NO": ("Meny", "89.00", 0),
                "SPAR_NO": ("Spar", "89.90", 1),
                "JOKER_NO": ("Joker", "99.90", 0),
            },
        ),
    ]
    answer = _answers(fake).grocery_price("kakaogård sjokoladedrikk")
    assert [c["name"] for c in answer["choices"]] == [
        "Sjokoladedrikk 450g boks Kakaogård",
        "Kakaogård Sjokoladedrikk Fjellbris",
    ]


class _ManyMatches:
    """A source whose search finds more products than one question should rank."""

    name = "kassalapp"

    def __init__(self, count):
        self.found = [Product(f"70000000{n:05d}", f"Lettmelk {n}") for n in range(count)]
        self.asked_prices = []

    def find(self, _words):
        return self.found

    def prices(self, products):
        self.asked_prices.append(list(products))
        return []

    def remember(self, words, product):
        pass


def test_ranking_the_choices_costs_at_most_one_price_call():
    """A broad name could match a hundred products; ranking them all would cost a call per hundred."""
    source = _ManyMatches(150)
    answer = PriceAnswers(source, today=lambda: TODAY).grocery_price("lettmelk")
    assert answer["outcome"] == "ask"
    assert [len(asked) for asked in source.asked_prices] == [20]
    assert [c["name"] for c in answer["choices"]] == ["Lettmelk 0", "Lettmelk 1", "Lettmelk 2"]


@pytest.mark.parametrize(
    "body",
    ["not json at all", '{"data": "garbage"}', '{"data": [17, null, {"stores": "none"}]}'],
    ids=["not json", "data a string", "entries of the wrong kind"],
)
def test_a_ranking_answer_that_cannot_be_read_still_asks_the_question(fake, body):
    """However the price answer is broken, a question it only ranks is still asked."""
    real = fake.transport()

    def bulk_broken(request):
        if request.url.path.endswith("/prices-bulk"):
            return httpx.Response(200, text=body)
        return real.handle_request(request)

    client = httpx.Client(base_url=fake.client().base_url, transport=httpx.MockTransport(bulk_broken))
    answer = PriceAnswers(KassalappSource(client, lambda: FAKE_KEY), today=lambda: TODAY).grocery_price(
        "lettmelk"
    )
    assert answer["outcome"] == "ask"
    assert answer["choices"][0]["name"] == "Grønnlund Lettmelk økologisk 1 l"
