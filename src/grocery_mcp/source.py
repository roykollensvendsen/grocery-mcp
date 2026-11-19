"""What every price source gives the answers: products, prices, and the ways a source fails.

A source fetches and translates; all the choosing (asking when a product is
unclear, how old a price may be, and what is said) lives in the answers, so
every source shares those rules. Kassalapp is the first, in ``sources/``.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - the dataclasses below read it at run time
import re
from dataclasses import dataclass
from decimal import Decimal  # noqa: TC003 - the dataclasses below read it at run time
from typing import Protocol


def plain_digits(text: str) -> bool:
    """Whether `text` is only the digits 0 to 9.

    Python's isdigit also says yes to digits such as "²" or Arabic-Indic ones, and
    int() refuses "²". A number read from outside is read only from these.
    """
    return re.fullmatch("[0-9]+", text) is not None


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
