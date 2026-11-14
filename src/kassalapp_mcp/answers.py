"""What the voice hears: the choosing, the price's age and the sentence.

The price source only fetches and translates. Everything said is decided here,
so a second source later gets the same rules without writing them again.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from kassalapp_mcp.source import KeyRefusedError, NotSetUpError, SearchTooShortError, SlowDownError

if TYPE_CHECKING:
    from collections.abc import Callable

    from kassalapp_mcp.source import ItemPrice, PriceSource, Product

#: Kassalapp calls a price out of date when it has not been checked for this long.
STALE_AFTER = dt.timedelta(days=14)
#: The most products named in one question, and the most chains in one answer.
MOST_CHOICES = 3
MOST_CHAINS = 3
#: Chains Kassalapp no longer fetches, by the words a person would use for them.
NOT_FETCHED = ("kiwi", "rema", "coop", "extra", "prix", "obs", "mega")
NOT_FETCHED_SAID = "No current prices from Kiwi, REMA or Coop."
PER_CHAIN = "Kassalapp's prices are per chain, not per shop."

NOT_SET_UP = "Kassalapp isn't set up; add your own key, see the README."
REFUSED = "Kassalapp refused the key; make a new one at kassal.app/profil/api and put it in the key file."
SLOW_DOWN = "Kassalapp has asked us to slow down; ask again in a minute."
TOO_SHORT = "Say at least three letters of the product."
MONTHS: tuple[str, ...] = ("January", "February", "March", "April", "May", "June", "July", "August")
MONTHS += ("September", "October", "November", "December")
#: Said in English whatever the machine's language, so the words do not change with it.
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class PriceAnswers:
    """The price tool's answers, for one voice session."""

    def __init__(self, source: PriceSource, today: Callable[[], dt.date]) -> None:
        """Answer from ``source``, ageing each price against ``today()``."""
        self._source = source
        self._today = today
        self._said_per_chain = False

    def grocery_price(self, product: str, chain: str | None = None) -> dict[str, Any]:
        """The price of a product at one chain, or at every chain that has it."""
        try:
            return self._price(product, chain)
        except NotSetUpError:
            return _refused("not_set_up", NOT_SET_UP)
        except KeyRefusedError:
            return _refused("refused", REFUSED)
        except SlowDownError:
            return _refused("slow_down", SLOW_DOWN)
        except SearchTooShortError:
            return _refused("too_short", TOO_SHORT)

    def _price(self, words: str, chain: str | None) -> dict[str, Any]:
        found = self._source.find(words)
        if not found:
            return _refused("not_found", f"Kassalapp has no product called {words}.")
        # RULE: several matches give a question never a guess
        if len(found) > 1:
            choices = [p.name for p in found[:MOST_CHOICES]]
            return {
                "success": False,
                "outcome": "ask",
                "say": f"Which one: {_either(choices)}?",
                "choices": choices,
            }
        [only] = found
        prices = self._source.prices([only])
        answer = self._one_chain(only, prices, chain) if chain else self._every_chain(only, prices)
        if not self._said_per_chain:
            answer["say"] += " " + PER_CHAIN
            self._said_per_chain = True
        return answer

    def _one_chain(self, product: Product, prices: list[ItemPrice], chain: str) -> dict[str, Any]:
        spoken = chain.strip().lower()
        at = next((p for p in prices if _is_chain(p, spoken)), None)
        if spoken.startswith(NOT_FETCHED):
            last = f"the last one is from {self._month(at.seen_at)}" if at and at.seen_at else "it has none"
            say = f"Kassalapp has no current prices from {chain.strip()}; {last}."
            return _answer("no_current_price", say, [at] if at else [], success=False)
        if at is None:
            return _answer("no_current_price", f"Kassalapp has no price for {product.name} at {chain}.", [])
        if not self._current(at):
            when = self._month(at.seen_at) if at.seen_at else "an unknown day"
            say = f"For {product.name} at {at.chain_name}, Kassalapp has only the last known price"
            say += f", from {when}. It was {at.price:.2f}."
            return _answer("no_current_price", say, [at], success=False)
        say = f"{product.name} costs {at.price:.2f} at {at.chain_name}, {self._checked(at)}."
        return _answer("price", say, [at])

    def _every_chain(self, product: Product, prices: list[ItemPrice]) -> dict[str, Any]:
        # RULE: a price older than fourteen days is never called current
        current = sorted((p for p in prices if self._current(p)), key=lambda p: p.price)
        if not current:
            say = f"Kassalapp has no current price for {product.name} at any chain. {NOT_FETCHED_SAID}"
            return _answer("no_current_price", say, [], success=False)
        named = [f"{p.price:.2f} at {p.chain_name}, {self._checked(p)}" for p in current[:MOST_CHAINS]]
        more = len(current) - MOST_CHAINS
        tail = f", and {more} more" if more > 0 else ""
        say = f"{product.name} costs {_both(named)}{tail}. {NOT_FETCHED_SAID}"
        return _answer("price", say, current)

    def _current(self, price: ItemPrice) -> bool:
        return price.seen_at is not None and self._today() - price.seen_at <= STALE_AFTER

    def _checked(self, price: ItemPrice) -> str:
        """How long ago a current price was checked, as it is said aloud."""
        assert price.seen_at is not None  # noqa: S101 - only current prices get here
        days = (self._today() - price.seen_at).days
        if days <= 0:
            return "checked today"
        if days == 1:
            return "checked yesterday"
        if days < 7:  # noqa: PLR2004 - within a week, the weekday is clear
            return f"checked on {WEEKDAYS[price.seen_at.weekday()]}"
        return f"checked on {price.seen_at.day} {MONTHS[price.seen_at.month - 1]}"

    def _month(self, day: dt.date) -> str:
        month = MONTHS[day.month - 1]
        return month if day.year == self._today().year else f"{month} {day.year}"


def _is_chain(price: ItemPrice, spoken: str) -> bool:
    return price.chain_name.lower() == spoken or price.chain.lower().removesuffix("_no") == spoken


def _answer(outcome: str, say: str, prices: list[ItemPrice], *, success: bool = True) -> dict[str, Any]:
    return {
        "success": success,
        "outcome": outcome,
        "say": say,
        "prices": [
            {
                "chain": p.chain_name,
                "price": f"{p.price:.2f}",
                "checked": p.seen_at.isoformat() if p.seen_at else None,
                "source": p.source,
            }
            for p in prices
        ],
    }


def _refused(outcome: str, say: str) -> dict[str, Any]:
    return {"success": False, "outcome": outcome, "say": say}


def _either(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _both(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + ", and " + parts[-1]
