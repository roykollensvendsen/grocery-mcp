"""A pretend Kassalapp, for tests and the simulated mode.

Its products and chains are made up, as Kassalapp's terms ask: no extract of
the real database is published. It keeps the real service's limits, so the
plugin cannot pass here and fail there:

* search is fuzzy, needs at least three characters, and its price has no date;
* bulk prices may come as strings;
* a wrong key gets 401, and too many calls get 429 with ``Retry-After``;
* Kiwi's price is from 2024, as Kassalapp no longer fetches that chain.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from kassalapp_mcp.source import BASE_URL, LIMIT_PER_MINUTE, MAX_BULK, MIN_SEARCH

#: The key the fake accepts. Any other gets 401.
FAKE_KEY = "fake-key"


@dataclass
class FakeProduct:
    """One made-up product, and what each chain last charged for it."""

    ean: str
    name: str
    #: chain code -> (chain name, price, days since it was checked)
    prices: dict[str, tuple[str, str, int]]


def _catalogue() -> list[FakeProduct]:
    return [
        FakeProduct(
            "7000000000017",
            "Fjellmelk lett 1 l",
            {
                "MENY_NO": ("Meny", "26.90", 0),
                "JOKER_NO": ("Joker", "24.90", 1),
                "SPAR_NO": ("Spar", "25.50", 15),
                "KIWI": ("Kiwi", "21.90", 600),
            },
        ),
        FakeProduct("7000000000024", "Fjellmelk hel 1 l", {"MENY_NO": ("Meny", "27.90", 0)}),
        FakeProduct("7000000000031", "Fjellmelk skummet 1 l", {"MENY_NO": ("Meny", "25.90", 0)}),
        FakeProduct("7000000000048", "Fjellmelk laktosefri 1 l", {"MENY_NO": ("Meny", "31.90", 0)}),
        FakeProduct("7000000000055", "Bergkaffe filtermalt 500 g", {"JOKER_NO": ("Joker", "79.90", 0)}),
    ]


@dataclass
class FakeKassalapp:
    """The service itself: answers requests and counts them."""

    today: dt.date = field(default_factory=dt.date.today)
    products: list[FakeProduct] = field(default_factory=_catalogue)
    #: Set to a number of seconds to answer the next calls with 429.
    retry_after: int | None = None
    #: False sends that 429 without a Retry-After header.
    retry_after_header: bool = True
    #: Calls left this minute, as the headers say; None leaves the headers out.
    remaining: int | None = LIMIT_PER_MINUTE
    #: Set to an HTTP status, such as 503, to answer every call with that error.
    fail_with: int | None = None
    #: Headers the real service might add one day, such as a daily limit.
    extra_headers: dict[str, str] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        """An HTTP transport that answers from this fake instead of the network."""
        return httpx.MockTransport(self._handle)

    def client(self) -> httpx.Client:
        """A client pointed at this fake, as the real one is pointed at Kassalapp."""
        return httpx.Client(base_url=BASE_URL, transport=self.transport())

    def _handle(self, request: httpx.Request) -> httpx.Response:
        response = self._answer(request)
        if self.remaining is not None:
            self.remaining = max(0, self.remaining - 1)
            response.headers["X-RateLimit-Limit"] = str(LIMIT_PER_MINUTE)
            response.headers["X-RateLimit-Remaining"] = str(self.remaining)
        response.headers.update(self.extra_headers)
        return response

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {FAKE_KEY}":
            return httpx.Response(401, json={"message": "Unauthenticated."})
        if self.fail_with is not None:
            return httpx.Response(self.fail_with, json={"message": "Service Unavailable"})
        if self.retry_after is not None:
            wait = {"Retry-After": str(self.retry_after)} if self.retry_after_header else {}
            return httpx.Response(429, headers=wait)
        path = request.url.path.removeprefix(httpx.URL(BASE_URL).path)
        if request.method == "GET" and path == "/products":
            return self._search(request.url.params)
        if request.method == "POST" and path == "/products/prices-bulk":
            return self._bulk(json.loads(request.content)["eans"])
        return httpx.Response(404, json={"message": "Not found."})

    def _search(self, params: httpx.QueryParams) -> httpx.Response:
        # As the live service, seen 2026-11-15: a yes/no setting is 1 or 0, never the word "true".
        bad = [f for f in ("unique", "exclude_without_ean") if params.get(f, "1") not in {"0", "1"}]
        if bad:
            return httpx.Response(422, json={"message": f"The {bad[0]} field must be true or false."})
        words = params.get("search", "")
        if len(words) < MIN_SEARCH:
            return httpx.Response(422, json={"message": "The search must be at least 3 characters."})
        # The real search is fuzzy: any word may match, and the best matches come first.
        wanted = words.lower().split()
        scored = [
            (sum(w in p.name.lower().split() for w in wanted), n, p) for n, p in enumerate(self.products)
        ]
        hits = [p for score, _, p in sorted(scored, key=lambda t: (-t[0], t[1])) if score][:10]
        # The real search gives a plain number with no date, and often no store.
        data = [
            {"id": n, "name": p.name, "ean": p.ean, "current_price": 0.0, "store": None}
            for n, p in enumerate(hits)
        ]
        return httpx.Response(200, json={"data": data, "links": {}, "meta": {}})

    def _bulk(self, eans: list[str]) -> httpx.Response:
        if not 1 <= len(eans) <= MAX_BULK:
            return httpx.Response(422, json={"message": "Between 1 and 100 EANs."})
        data: list[dict[str, Any]] = []
        for product in self.products:
            if product.ean not in eans:
                continue
            stores = [
                {
                    "store": code,
                    "name": name,
                    "current_price": price,  # a string, as the real service sends
                    "current_unit_price": None,
                    "last_checked": (self.today - dt.timedelta(days=age)).isoformat() + " 10:00:00",
                }
                for code, (name, price, age) in product.prices.items()
            ]
            data.append({"ean": product.ean, "name": product.name, "stores": stores, "price_history": []})
        return httpx.Response(200, json={"data": data, "meta": {"requested_eans": len(eans)}})
