"""Kassalapp as a price source: it calls the API and translates the answer.

All the choosing (asking when a product is unclear, how old a price may be, and
what is said) lives in the tools, so another price source can share them.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import pathlib

    import httpx

BASE_URL = "https://kassal.app/api/v1"
#: Seconds one call may take. The voice gives up on a tool after about 15.
TIMEOUT = 5.0
#: Kassalapp refuses a search shorter than this.
MIN_SEARCH = 3
#: Kassalapp's limit on barcodes in one bulk price call.
MAX_BULK = 100


@dataclass(frozen=True)
class Product:
    """A product a few spoken words could mean."""

    ean: str
    name: str


@dataclass(frozen=True)
class ItemPrice:
    """What one chain last charged for one product, and when that was checked."""

    product: Product
    chain: str
    chain_name: str
    price: Decimal
    seen_at: dt.date | None
    source: str = "kassalapp"


class PriceSource(Protocol):
    """Where prices come from. Kassalapp is the first; others share the tools."""

    name: str

    def find(self, words: str) -> list[Product]:
        """The products the spoken words could mean."""
        ...

    def prices(self, products: list[Product]) -> list[ItemPrice]:
        """Each chain's latest price for each product."""
        ...


class NotSetUpError(Exception):
    """There is no key, so nothing may be sent."""


class KeyRefusedError(Exception):
    """Kassalapp refused the key."""


class SlowDownError(Exception):
    """Kassalapp asked us to wait before the next call."""

    def __init__(self, retry_after: int | None) -> None:
        """Keep how long Kassalapp asked us to wait, if it said."""
        super().__init__(f"retry after {retry_after}")
        self.retry_after = retry_after


class SearchTooShortError(Exception):
    """The spoken words are too short for Kassalapp to search."""


class KassalappSource:
    """The Kassalapp API, read-only."""

    name = "kassalapp"

    def __init__(self, client: httpx.Client, key_file: pathlib.Path) -> None:
        """Use ``client`` for calls, with the key read from ``key_file`` on each call."""
        self._client = client
        self._key_file = key_file

    def find(self, words: str) -> list[Product]:
        """The products the spoken words could mean."""
        words = " ".join(words.split())
        # RULE: a search shorter than kassalapp allows is never sent
        if len(words) < MIN_SEARCH:
            raise SearchTooShortError(words)
        params = {"search": words, "exclude_without_ean": "true", "unique": "true", "size": "10"}
        body = self._get("/products", params)
        return [Product(str(p["ean"]), str(p["name"])) for p in body["data"] if p.get("ean")]

    def prices(self, products: list[Product]) -> list[ItemPrice]:
        """Each chain's latest price for each product."""
        by_ean = {p.ean: p for p in products}
        found: list[ItemPrice] = []
        eans = list(by_ean)
        for start in range(0, len(eans), MAX_BULK):
            body = self._post("/products/prices-bulk", {"eans": eans[start : start + MAX_BULK]})
            for entry in body["data"]:
                product = by_ean.get(str(entry["ean"]))
                if product is None:
                    continue
                found.extend(
                    ItemPrice(
                        product=product,
                        chain=str(store["store"]),
                        chain_name=str(store["name"]),
                        # Kassalapp sends a price as a number or a string; both read exactly.
                        price=Decimal(str(store["current_price"])),
                        seen_at=_date(store.get("last_checked")),
                    )
                    for store in entry["stores"]
                    if store.get("current_price") is not None
                )
        return found

    def _headers(self) -> dict[str, str]:
        try:
            key = self._key_file.read_text().strip()
        except FileNotFoundError:
            key = ""
        # RULE: without a key nothing is sent
        if not key:
            raise NotSetUpError(str(self._key_file))
        return {"Authorization": f"Bearer {key}", "Accept": "application/json"}

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        return _checked(self._client.get(path, params=params, headers=self._headers(), timeout=TIMEOUT))

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return _checked(self._client.post(path, json=body, headers=self._headers(), timeout=TIMEOUT))


def _checked(response: httpx.Response) -> dict[str, Any]:
    """The answer's body, or the error that says what Kassalapp refused."""
    if response.status_code == HTTPStatus.UNAUTHORIZED:
        raise KeyRefusedError
    if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
        wait = response.headers.get("Retry-After", "")
        raise SlowDownError(int(wait) if wait.isdigit() else None)
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return body


def _date(value: object) -> dt.date | None:
    """The day a price was last checked, from Kassalapp's date and time."""
    if not isinstance(value, str) or len(value) < len("2026-11-14"):
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None
