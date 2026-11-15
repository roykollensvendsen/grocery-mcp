"""The bulk price answer as Kassalapp describes it, read without trusting every field.

The shape is Kassalapp's own public description, kassal.app/docs/api.json, read on
2026-11-14 (schema BulkPriceHistoryStoreResource): each store has ``store``, a code
such as "MENY_NO", a display ``name``, ``current_price`` as a number or null, and
``last_checked`` as a string or null. The product here is made up. A recorded live
answer is still owed; it replaces this one when the owner runs the one live call.
"""

import json

import httpx

from kassalapp_mcp.source import BASE_URL, KassalappSource, Product

COFFEE = Product("7000000000055", "Bergkaffe filtermalt 500 g")


def _source(body):
    def handle(request):
        assert request.url.path.endswith("/products/prices-bulk")
        return httpx.Response(200, json=body)

    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handle))
    return KassalappSource(client, lambda: "made-up-key")


def _bulk(stores):
    return {"data": [{"ean": COFFEE.ean, "name": COFFEE.name, "stores": stores, "price_history": []}]}


def test_the_bulk_answer_as_kassalapp_describes_it_is_read():
    store = {
        "store": "MENY_NO",
        "name": "Meny",
        "current_price": 79.9,
        "current_unit_price": 159.8,
        "current_unit_price_unit": "kg",
        "last_checked": "2026-11-14T08:12:44.000000Z",
    }
    [price] = _source(_bulk([store])).prices([COFFEE])
    assert (price.chain, price.chain_name, str(price.price)) == ("MENY_NO", "Meny", "79.9")
    assert price.seen_at.isoformat() == "2026-11-14"


def test_a_store_kassalapp_sends_as_an_object_is_read_by_its_code():
    store = {"store": {"code": "JOKER_NO", "name": "Joker"}, "current_price": "24.90", "last_checked": None}
    [price] = _source(_bulk([store])).prices([COFFEE])
    assert (price.chain, price.chain_name, price.seen_at) == ("JOKER_NO", "Joker", None)


def test_a_store_with_fields_missing_is_left_out_rather_than_breaking_the_answer():
    stores = [{"name": "Nameless", "current_price": 1.0}, {"store": "SPAR_NO", "current_price": "x"}]
    assert _source(_bulk(stores)).prices([COFFEE]) == []
    assert _source({"data": [{"ean": COFFEE.ean}]}).prices([COFFEE]) == []
    json.dumps(_bulk(stores))  # the shapes above are plain JSON, as a recorded answer will be
