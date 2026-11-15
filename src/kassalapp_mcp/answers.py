"""What the voice hears: the choosing, the price's age and the sentence.

The price source only fetches and translates. Everything said is decided here,
so a second source later gets the same rules without writing them again.
"""

from __future__ import annotations

import datetime as dt
import unicodedata
from typing import TYPE_CHECKING, Any

import httpx

from kassalapp_mcp.cache import normalised
from kassalapp_mcp.source import (
    KeyRefusedError,
    NotSetUpError,
    Product,
    SearchTooShortError,
    SlowDownError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from kassalapp_mcp.source import ItemPrice, PriceSource

#: Kassalapp calls a price out of date when it has not been checked for this long.
STALE_AFTER = dt.timedelta(days=14)
#: The most products named in one question, and the most chains in one answer.
MOST_CHOICES = 3
MOST_CHAINS = 3
#: The most matches whose prices are asked, to rank a question's choices in one call.
MOST_RANKED = 20
#: The most characters of one name read aloud: a product, a chain, or the words asked with.
SPOKEN_MAX = 60
#: A barcode, as the voice may pass back one of the choices it was offered.
BARCODE_DIGITS = range(8, 15)
#: Chains Kassalapp no longer fetches, by the words a person would use for them.
NOT_FETCHED = ("kiwi", "rema", "coop", "extra", "prix", "obs", "mega")
NOT_FETCHED_SAID = "No current prices from Kiwi, REMA or Coop."
PER_CHAIN = "Kassalapp's prices are per chain, not per shop."
#: Words around a chain's name that do not make it a particular shop.
FILLER = frozenset({"at", "the"})

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
        #: The words of the last question that asked "which one", and what it offered.
        self._asked: tuple[str, list[Product]] | None = None

    def grocery_price(self, product: str, chain: str | None = None) -> dict[str, Any]:
        """The price of a product at one chain, or at every chain that has it."""

        def build(only: Product, prices: list[ItemPrice]) -> dict[str, Any]:
            return self._one_chain(only, prices, chain) if chain else self._every_chain(only, prices)

        return self._answered(product, build)

    def grocery_cheapest(self, product: str) -> dict[str, Any]:
        """Which chain is cheapest for a product, among the current prices."""
        return self._answered(product, self._cheapest)

    def _answered(
        self, words: str, build: Callable[[Product, list[ItemPrice]], dict[str, Any]]
    ) -> dict[str, Any]:
        """Find the one product the words mean, then let ``build`` answer from its prices."""
        try:
            return self._resolved(words, build)
        except NotSetUpError:
            return _refused("not_set_up", NOT_SET_UP)
        except KeyRefusedError:
            return _refused("refused", REFUSED)
        except SlowDownError:
            return _refused("slow_down", SLOW_DOWN)
        except SearchTooShortError:
            return _refused("too_short", TOO_SHORT)

    def _resolved(
        self, words: str, build: Callable[[Product, list[ItemPrice]], dict[str, Any]]
    ) -> dict[str, Any]:
        chosen = self._chosen(words)
        if isinstance(chosen, dict):
            return chosen
        prices = self._source.prices([chosen])
        if not chosen.name:
            if not prices:
                return _refused("not_found", f"Kassalapp has no product with the barcode {spoken(words)}.")
            chosen = prices[0].product
        self._keep(words, chosen)
        answer = build(chosen, prices)
        if not self._said_per_chain:
            answer["say"] += " " + PER_CHAIN
            self._said_per_chain = True
        return answer

    def _chosen(self, words: str) -> Product | dict[str, Any]:
        """The one product the words mean, or the answer that asks which."""
        given = words.strip()
        if given.isdigit() and len(given) in BARCODE_DIGITS:
            offered = self._asked[1] if self._asked else []
            return next((p for p in offered if p.ean == given), Product(given, ""))
        found = self._source.find(words)
        if not found:
            return _refused(
                "not_found",
                f"Kassalapp has no product called {spoken(words)}. Try fewer words, or the brand.",
            )
        # RULE: answering an offered choice gives a price never the question again
        exact = [p for p in found if normalised(p.name) == normalised(words)]
        if len(found) == 1 or len(exact) == 1:
            return found[0] if len(found) == 1 else exact[0]
        # RULE: several matches give a question never a guess
        offered = self._most_sold_first(found)[:MOST_CHOICES]
        self._asked = (words, offered)
        return {
            "success": False,
            "outcome": "ask",
            "say": f"Which one: {_either([spoken(p.name) for p in offered])}?",
            "choices": [{"name": p.name, "ean": p.ean} for p in offered],
        }

    def _most_sold_first(self, found: list[Product]) -> list[Product]:
        """The matches, those sold now in the most chains first, ties in Kassalapp's order.

        Kassalapp's order says how well the words match, so a bare "lettmelk" can put
        an organic line sold in one chain before the milk every chain sells. One bulk
        price call counts the chains; with the cache, it also answers the price asked next.
        """
        # RULE: ranking the choices costs at most one price call
        ranked, rest = found[:MOST_RANKED], found[MOST_RANKED:]
        try:
            prices = self._source.prices(ranked)
        except (SlowDownError, httpx.HTTPError):
            # Kassalapp's own order still makes a fair question.
            # RULE: a failed ranking still asks the question
            return found
        chains: dict[str, set[str]] = {p.ean: set() for p in found}
        for price in prices:
            if self._current(price):
                chains.setdefault(price.product.ean, set()).add(price.chain)
        # RULE: the choices are offered most sold first
        return sorted(ranked, key=lambda p: -len(chains[p.ean])) + rest

    def _keep(self, words: str, chosen: Product) -> None:
        """Remember a chosen product under these words, and under the question that offered it."""
        if self._asked and any(p.ean == chosen.ean for p in self._asked[1]):
            self._source.remember(self._asked[0], chosen)
            self._asked = None
        if not words.strip().isdigit():
            self._source.remember(words, chosen)

    def _cheapest(self, product: Product, prices: list[ItemPrice]) -> dict[str, Any]:
        # RULE: a stale price is never the cheapest
        current = sorted((p for p in prices if self._current(p)), key=lambda p: (p.price, p.chain_name))
        if not current:
            say = (
                f"Kassalapp has no current price for {spoken(product.name)} at any chain. {NOT_FETCHED_SAID}"
            )
            return _answer("no_current_price", say, [], success=False)
        best = current[0]
        if len(current) == 1:
            say = f"Only {spoken(best.chain_name)} has a current price for {spoken(product.name)}:"
            say += f" {best.price:.2f}, {self._checked(best)}."
            return _answer("cheapest", f"{say} {NOT_FETCHED_SAID}", current)
        tied = [p for p in current if p.price == best.price]
        if len(tied) > 1:
            chains = _both_and([spoken(p.chain_name) for p in tied[:MOST_CHAINS]])
            say = f"{spoken(product.name)} costs the same, {best.price:.2f}, at {chains},"
            say += f" {self._checked(best)}."
            return _answer("cheapest", f"{say} {NOT_FETCHED_SAID}", current)
        after = current[1]
        say = f"{spoken(product.name)} is cheapest at {spoken(best.chain_name)}, {best.price:.2f},"
        say += f" {self._checked(best)}."
        say += f" Then {spoken(after.chain_name)} at {after.price:.2f}, {self._checked(after)}."
        return _answer("cheapest", f"{say} {NOT_FETCHED_SAID}", current)

    def _one_chain(self, product: Product, prices: list[ItemPrice], chain: str) -> dict[str, Any]:
        words = [w.strip(",.") for w in chain.lower().split()]
        # Said as the person said it, less a leading "at" the sentence already has.
        asked = " ".join(chain.split()[1:]) if words[:1] == ["at"] else chain
        # RULE: a shop named by place finds its chain and says prices are per chain
        at = next((p for p in prices if _is_chain(p, words)), None)
        not_fetched = next((w for w in words if w.startswith(NOT_FETCHED)), None)
        chain_words = set(at.chain_name.lower().split()) if at else {not_fetched}
        shop = any(w not in chain_words and w not in FILLER for w in words)
        answer = self._at_chain(product, at, not_fetched, asked)
        if shop and (at or not_fetched):
            answer["say"] += " " + PER_CHAIN
            self._said_per_chain = True
        return answer

    def _at_chain(
        self, product: Product, at: ItemPrice | None, not_fetched: str | None, asked: str
    ) -> dict[str, Any]:
        # Roy's decision: these chains are never scraped, so the voice says plainly there is no price.
        # RULE: a chain kassalapp no longer reads is said to have no current prices
        if not_fetched:
            name = at.chain_name if at else not_fetched.capitalize()
            last = f"the last one is from {self._month(at.seen_at)}" if at and at.seen_at else "it has none"
            say = f"Kassalapp has no current prices from {spoken(name)}; {last}."
            return _answer("no_current_price", say, [at] if at else [], success=False)
        if at is None:
            say = f"Kassalapp has no price for {spoken(product.name)} at {spoken(asked)}."
            return _answer("no_current_price", say, [])
        if not self._current(at):
            when = self._month(at.seen_at) if at.seen_at else "an unknown day"
            say = f"For {spoken(product.name)} at {spoken(at.chain_name)},"
            say += f" Kassalapp has only the last known price, from {when}. It was {at.price:.2f}."
            return _answer("no_current_price", say, [at], success=False)
        say = f"{spoken(product.name)} costs {at.price:.2f} at {spoken(at.chain_name)}, {self._checked(at)}."
        return _answer("price", say, [at])

    def _every_chain(self, product: Product, prices: list[ItemPrice]) -> dict[str, Any]:
        # RULE: a price older than fourteen days is never called current
        current = sorted((p for p in prices if self._current(p)), key=lambda p: p.price)
        if not current:
            say = (
                f"Kassalapp has no current price for {spoken(product.name)} at any chain. {NOT_FETCHED_SAID}"
            )
            return _answer("no_current_price", say, [], success=False)
        named = [
            f"{p.price:.2f} at {spoken(p.chain_name)}, {self._checked(p)}" for p in current[:MOST_CHAINS]
        ]
        more = len(current) - MOST_CHAINS
        tail = f", and {more} more" if more > 0 else ""
        say = f"{spoken(product.name)} costs {_both(named)}{tail}. {NOT_FETCHED_SAID}"
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


def spoken(text: str) -> str:
    """A name as it may be read aloud: one line, no control characters, and short."""
    # RULE: a name in a spoken sentence is one short line
    printable = "".join(c if unicodedata.category(c)[0] != "C" else " " for c in text)
    line = " ".join(printable.split())
    if len(line) <= SPOKEN_MAX:
        return line
    return line[: SPOKEN_MAX - 1].rsplit(" ", 1)[0] + "…"


def _is_chain(price: ItemPrice, words: list[str]) -> bool:
    """Whether the chain is named anywhere in the words, as in "the Meny in town"."""
    name = price.chain_name.lower().split()
    found = any(words[i : i + len(name)] == name for i in range(len(words)))
    return found or price.chain.lower().removesuffix("_no") in words


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


def _both_and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
