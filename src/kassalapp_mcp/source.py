"""Kassalapp as a price source: it calls the API and translates the answer.

All the choosing (asking when a product is unclear, how old a price may be, and
what is said) lives in the tools, so another price source can share them.
"""

from __future__ import annotations

import datetime as dt
import logging
import pathlib
import stat
from dataclasses import dataclass
from decimal import Decimal
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Protocol

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable

    from kassalapp_mcp.quota import Quota

BASE_URL = "https://kassal.app/api/v1"
# The HTTP library logs every address it calls, and those hold the search words,
# which are the user's shopping. Only its warnings are let through.
for _library in ("httpx", "httpcore"):
    # RULE: search words are never logged
    logging.getLogger(_library).setLevel(logging.WARNING)

log = logging.getLogger(__name__)

#: Limits on one call: two seconds to connect, four for each other phase. The whole
#: question has its own deadline as well, in the server.
TIMEOUT = httpx.Timeout(4.0, connect=2.0)
#: Kassalapp refuses a search shorter than this.
MIN_SEARCH = 3
#: Kassalapp's limit on barcodes in one bulk price call.
MAX_BULK = 100
#: Kassalapp's free plan: calls a minute, per account. Read on kassal.app/api, 2026-11-14.
LIMIT_PER_MINUTE = 60


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
        """Each chain's latest price for each product.

        A product given by barcode alone, with an empty name, gets its name from the source.
        """
        ...

    def remember(self, words: str, product: Product) -> None:
        """Keep the product chosen for these words, if the source keeps anything."""
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

    def __init__(
        self, client: httpx.Client, key: pathlib.Path | Callable[[], str], quota: Quota | None = None
    ) -> None:
        """Use ``client`` for calls, with the key read on each call from a file or a function.

        ``quota``, when given, may refuse a call before it is sent, and counts each one sent.
        """
        self._client = client
        self._key = key
        self._key_checked = False
        self._quota = quota

    def find(self, words: str) -> list[Product]:
        """The products the spoken words could mean."""
        words = " ".join(words.split())
        # RULE: a search shorter than kassalapp allows is never sent
        if len(words) < MIN_SEARCH:
            raise SearchTooShortError(words)
        # Kassalapp takes a yes/no setting as 1 or 0; the word "true" is refused with 422.
        params = {"search": words, "exclude_without_ean": "1", "unique": "1", "size": "10"}
        body = self._get("/products", params)
        return [Product(str(p["ean"]), str(p["name"])) for p in body["data"] if p.get("ean")]

    def prices(self, products: list[Product]) -> list[ItemPrice]:
        """Each chain's latest price for each product."""
        by_ean = {p.ean: p for p in products}
        found: list[ItemPrice] = []
        eans = list(by_ean)
        for start in range(0, len(eans), MAX_BULK):
            body = self._post("/products/prices-bulk", {"eans": eans[start : start + MAX_BULK]})
            for entry in body.get("data") or []:
                product = by_ean.get(str(entry.get("ean")))
                if product is None:
                    continue
                if not product.name:
                    product = Product(product.ean, str(entry.get("name") or product.ean))
                found.extend(
                    price for store in entry.get("stores") or [] if (price := _store_price(product, store))
                )
        return found

    def remember(self, words: str, product: Product) -> None:
        """Kassalapp itself keeps nothing; the cache in front of it does."""

    def _headers(self) -> dict[str, str]:
        key = _read_key(self._key)
        if isinstance(self._key, pathlib.Path) and key and not self._key_checked:
            self._key_checked = True
            _warn_if_shared(self._key)
        # RULE: without a key nothing is sent
        if not key:
            raise NotSetUpError
        return {"Authorization": f"Bearer {key}", "Accept": "application/json"}

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        headers = self._headers()
        return self._counted(
            path, lambda: self._client.get(path, params=params, headers=headers, timeout=TIMEOUT)
        )

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = self._headers()
        return self._counted(
            path, lambda: self._client.post(path, json=body, headers=headers, timeout=TIMEOUT)
        )

    def _counted(self, path: str, send: Callable[[], httpx.Response]) -> dict[str, Any]:
        """Send one call, if the quota allows it, and count it by ``path``, a template with no words."""
        if self._quota is not None:
            self._quota.before_call()
        response = send()
        if self._quota is not None:
            # RULE: the usage log never holds a search word or barcode
            self._quota.after_call(path, response)
        return _checked(response)


def _store_price(product: Product, store: object) -> ItemPrice | None:
    """One chain's price, or None when Kassalapp left out what is needed to say it.

    Kassalapp's description gives ``store`` as a code string and ``name`` beside it.
    An object with ``code`` and ``name`` is read too, in case the answer differs.
    """
    if not isinstance(store, dict):
        return None
    code: object = store.get("store")
    name: object = store.get("name")
    if isinstance(code, dict):
        name = name or code.get("name")
        code = code.get("code")
    try:
        # Kassalapp sends a price as a number or a string; both read exactly.
        price = Decimal(str(store.get("current_price")))
    except ArithmeticError:
        return None
    if not isinstance(code, str) or not code or not price.is_finite():
        return None
    return ItemPrice(
        product=product,
        chain=code,
        chain_name=str(name or code),
        price=price,
        seen_at=_date(store.get("last_checked")),
    )


def _warn_if_shared(path: pathlib.Path) -> None:
    """Warn, once, when others on the machine may read the key file."""
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        log.warning("The key file %s can be read by others (mode %o); run: chmod 600 %s", path, mode, path)


def _read_key(key: pathlib.Path | Callable[[], str]) -> str:
    if not isinstance(key, pathlib.Path):
        return key().strip()
    try:
        return key.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


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
