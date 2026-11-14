"""Kassalapp as a price source, against the fake service."""

import datetime as dt
from decimal import Decimal

import pytest

from kassalapp_mcp.fake import FAKE_KEY, FakeKassalapp
from kassalapp_mcp.source import (
    KassalappSource,
    KeyRefusedError,
    NotSetUpError,
    Product,
    SearchTooShortError,
    SlowDownError,
)

TODAY = dt.date(2026, 11, 14)
LETT = Product("7000000000017", "Fjellmelk lett 1 l")


@pytest.fixture
def fake():
    return FakeKassalapp(today=TODAY)


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "key"
    path.write_text(FAKE_KEY + "\n")
    return path


def test_find_gives_each_product_the_words_could_mean(fake, key_file):
    source = KassalappSource(fake.client(), key_file)
    found = source.find("fjellmelk lett")
    assert found == [LETT]


def test_find_asks_kassalapp_for_products_with_a_barcode_once_each(fake, key_file):
    KassalappSource(fake.client(), key_file).find("fjellmelk")
    params = fake.requests[0].url.params
    assert params["exclude_without_ean"] == "true"
    assert params["unique"] == "true"


def test_a_search_shorter_than_kassalapp_allows_is_never_sent(fake, key_file):
    with pytest.raises(SearchTooShortError):
        KassalappSource(fake.client(), key_file).find("ab")
    assert fake.requests == []


def test_prices_give_each_chain_with_the_date_it_was_checked(fake, key_file):
    prices = KassalappSource(fake.client(), key_file).prices([LETT])
    meny = next(p for p in prices if p.chain == "MENY_NO")
    assert (meny.chain_name, meny.price, meny.seen_at) == ("Meny", Decimal("26.90"), TODAY)
    kiwi = next(p for p in prices if p.chain == "KIWI")
    assert kiwi.seen_at == TODAY - dt.timedelta(days=600)


def test_without_a_key_nothing_is_sent(fake, tmp_path):
    source = KassalappSource(fake.client(), tmp_path / "missing")
    with pytest.raises(NotSetUpError):
        source.find("fjellmelk")
    with pytest.raises(NotSetUpError):
        source.prices([LETT])
    assert fake.requests == []


def test_a_wrong_key_is_reported_as_refused(fake, tmp_path):
    wrong = tmp_path / "key"
    wrong.write_text("not-the-key")
    with pytest.raises(KeyRefusedError):
        KassalappSource(fake.client(), wrong).find("fjellmelk")


def test_a_request_to_slow_down_keeps_how_long_to_wait(fake, key_file):
    fake.retry_after = 30
    with pytest.raises(SlowDownError) as raised:
        KassalappSource(fake.client(), key_file).prices([LETT])
    assert raised.value.retry_after == 30


def test_the_source_only_reads(fake, key_file):
    source = KassalappSource(fake.client(), key_file)
    source.find("fjellmelk")
    source.prices([LETT])
    paths = {(r.method, r.url.path) for r in fake.requests}
    assert paths == {("GET", "/api/v1/products"), ("POST", "/api/v1/products/prices-bulk")}
