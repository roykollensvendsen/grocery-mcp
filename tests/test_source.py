"""Kassalapp as a price source, against the fake service."""

import datetime as dt
from decimal import Decimal

import pytest

from grocery_mcp.fake import FAKE_KEY, FakeKassalapp
from grocery_mcp.source import KeyRefusedError, NotSetUpError, Product, SearchTooShortError, SlowDownError
from grocery_mcp.sources.kassalapp import KassalappSource

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
    found = source.find("bergkaffe")
    assert found == [Product("7000000000055", "Bergkaffe filtermalt 500 g")]


def test_find_asks_kassalapp_for_products_with_a_barcode_once_each(fake, key_file):
    KassalappSource(fake.client(), key_file).find("fjellmelk")
    params = fake.requests[0].url.params
    assert params["exclude_without_ean"] == "1"
    assert params["unique"] == "1"


def test_the_fake_refuses_a_yes_no_setting_sent_as_a_word_as_kassalapp_does(fake):
    headers = {"Authorization": f"Bearer {FAKE_KEY}"}
    response = fake.client().get(
        "/products", params={"search": "bergkaffe", "unique": "true"}, headers=headers
    )
    assert response.status_code == 422


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


def test_a_key_file_others_can_read_is_warned_about_once(fake, key_file, caplog):
    key_file.chmod(0o644)
    source = KassalappSource(fake.client(), key_file)
    with caplog.at_level("WARNING", logger="grocery_mcp.sources.kassalapp"):
        source.find("fjellmelk")
        source.find("fjellmelk")
    assert len(caplog.records) == 1
    assert "chmod 600" in caplog.text
    assert FAKE_KEY not in caplog.text


def test_a_key_file_only_its_owner_can_read_is_not_warned_about(fake, key_file, caplog):
    key_file.chmod(0o600)
    with caplog.at_level("WARNING", logger="grocery_mcp.sources.kassalapp"):
        KassalappSource(fake.client(), key_file).find("fjellmelk")
    assert caplog.records == []


def test_a_slow_down_whose_wait_is_not_plain_digits_has_no_wait(fake, key_file):
    fake.retry_after = 0
    fake.retry_after_header = False
    fake.extra_headers = {"Retry-After": b"\xb2"}  # "²", as test_quota.py's SQUARED
    with pytest.raises(SlowDownError) as raised:
        KassalappSource(fake.client(), key_file).prices([LETT])
    assert raised.value.retry_after is None
