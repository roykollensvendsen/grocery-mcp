"""A cache in front of a price source, so the same question never costs twice.

Kassalapp fetches prices once a day, starting around 09:00 and taking a couple
of hours, and asks callers to keep answers for six hours. So a price is kept
six hours, and one fetched before 09:00 only until 10:00, when the new day's
prices start to arrive. Words that found exactly one product are kept for good.

The cache lives on the user's own machine and is never published: Kassalapp's
terms forbid putting out an extract of its prices.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import threading
from decimal import Decimal
from typing import TYPE_CHECKING

from kassalapp_mcp.source import ItemPrice, Product, SlowDownError

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable

    from kassalapp_mcp.source import PriceSource

#: How long Kassalapp asks a price to be kept.
KEEP_PRICE = dt.timedelta(hours=6)
#: Before this hour the day's new prices have not started; until the next they may be arriving.
UPDATE_STARTS = dt.time(9, 0)
UPDATE_SETTLED = dt.time(10, 0)
_UNITS = {"liter": "l", "litre": "l", "l": "l", "ml": "ml", "gram": "g", "g": "g", "kilo": "kg", "kg": "kg"}
_AMOUNT = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")\b")


def normalised(words: str) -> str:
    """Spoken words in one form, so the same question finds the same entry."""
    folded = " ".join(words.lower().split())
    return _AMOUNT.sub(lambda m: m.group(1) + _UNITS[m.group(2)], folded)


def kept_until(fetched: dt.datetime) -> dt.datetime:
    """When a price fetched at ``fetched`` must be fetched again."""
    # RULE: a price fetched before nine is held only until ten
    if fetched.timetz().replace(tzinfo=None) < UPDATE_STARTS:
        return fetched.replace(hour=UPDATE_SETTLED.hour, minute=0, second=0, microsecond=0)
    return fetched + KEEP_PRICE


class CachedSource:
    """A price source that remembers what it was told."""

    def __init__(
        self,
        inner: PriceSource,
        path: pathlib.Path | str,
        now: Callable[[], dt.datetime],
        on_hit: Callable[[], None] = lambda: None,
    ) -> None:
        """Ask ``inner`` only for what the cache at ``path`` lacks; call ``on_hit`` when it lacks nothing."""
        self.name = inner.name
        self._inner = inner
        self._now = now
        self._on_hit = on_hit
        self._lock = threading.Lock()
        # The MCP library may run a tool on a worker thread; the lock keeps one writer at a time.
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        with self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS words (words TEXT PRIMARY KEY, ean TEXT, name TEXT)")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS prices (ean TEXT PRIMARY KEY, until TEXT, rows TEXT)"
            )

    def find(self, words: str) -> list[Product]:
        """The products the spoken words could mean."""
        key = normalised(words)
        with self._lock:
            row = self._db.execute("SELECT ean, name FROM words WHERE words = ?", (key,)).fetchone()
        if row:
            self._on_hit()
            return [Product(row[0], row[1])]
        found = self._inner.find(words)
        if len(found) == 1:
            with self._lock, self._db:
                self._db.execute("INSERT OR REPLACE INTO words VALUES (?, ?, ?)", (key, *_pair(found[0])))
        return found

    def remember(self, words: str, product: Product) -> None:
        """Keep the product chosen for these words, so the same words ask nothing next time."""
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO words VALUES (?, ?, ?)", (normalised(words), *_pair(product))
            )

    def prices(self, products: list[Product]) -> list[ItemPrice]:
        """Each chain's latest price for each product."""
        now = self._now()
        kept: list[ItemPrice] = []
        missing: list[Product] = []
        old: dict[str, list[ItemPrice]] = {}
        for product in products:
            with self._lock:
                row = self._db.execute(
                    "SELECT until, rows FROM prices WHERE ean = ?", (product.ean,)
                ).fetchone()
            # RULE: a cached price is used for six hours
            if row and now < dt.datetime.fromisoformat(row[0]):
                kept.extend(_loaded(product, row[1]))
            else:
                missing.append(product)
                if row:
                    old[product.ean] = _loaded(product, row[1])
        if not missing:
            self._on_hit()
            return kept
        try:
            fetched = self._inner.prices(missing)
        except SlowDownError:
            # While Kassalapp asks us to wait, an old answer still says its own age.
            if any(p.ean not in old for p in missing):
                raise
            self._on_hit()
            return kept + [price for p in missing for price in old[p.ean]]
        until = kept_until(now).isoformat()
        with self._lock, self._db:
            for product in missing:
                rows = [p for p in fetched if p.product.ean == product.ean]
                self._db.execute(
                    "INSERT OR REPLACE INTO prices VALUES (?, ?, ?)", (product.ean, until, _dumped(rows))
                )
        return kept + fetched


def _pair(product: Product) -> tuple[str, str]:
    return product.ean, product.name


def _dumped(prices: list[ItemPrice]) -> str:
    return json.dumps(
        [
            {
                "chain": p.chain,
                "chain_name": p.chain_name,
                "price": str(p.price),
                "seen_at": p.seen_at.isoformat() if p.seen_at else None,
                "source": p.source,
            }
            for p in prices
        ]
    )


def _loaded(product: Product, rows: str) -> list[ItemPrice]:
    return [
        ItemPrice(
            product=product,
            chain=r["chain"],
            chain_name=r["chain_name"],
            price=Decimal(r["price"]),
            seen_at=dt.date.fromisoformat(r["seen_at"]) if r["seen_at"] else None,
            source=r["source"],
        )
        for r in json.loads(rows)
    ]
