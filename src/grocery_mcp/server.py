"""The MCP server: the price tool, over stdio, for a voice session."""

from __future__ import annotations

import concurrent.futures
import functools
import logging
import traceback
from typing import TYPE_CHECKING, Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

if TYPE_CHECKING:
    from collections.abc import Callable

    from grocery_mcp.answers import PriceAnswers

log = logging.getLogger(__name__)

#: The Claude app shows about this many characters of a tool before loading it.
FIRST_LINE_MAX = 26
INSTRUCTIONS = (
    "Grocery prices per chain from Kassalapp. Read each 'say' aloud as it is. "
    "When the answer asks which product, read the choices and wait; never pick one. "
    "Then pass the chosen name or its 'ean' as 'product'. "
    "It only reads prices and never buys anything."
)
SIMULATED = "SIMULATED prices, not real ones: say so. "
#: What the voice hears when Kassalapp cannot be reached.
DROPPED = "Kassalapp did not answer; try again in a minute."
#: What the voice hears when the plugin itself fails, so it never hears only that the tool failed.
BROKE = "That went wrong inside grocery-mcp. Try again, and if it fails again, check its log."
#: Seconds one question may take, both calls to Kassalapp together. The voice gives up near 15.
ANSWER_WITHIN = 9.0
#: What the voice hears at that deadline; the work carries on, and fills the cache.
SLOW = "Kassalapp is slow to answer; ask again in a moment."
#: Questions run here, so one past its deadline can finish while the voice is answered.
_WORKERS = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="kassalapp")
#: What the voice hears when Kassalapp answers, but with an error of its own.
KASSALAPP_ERROR = "Kassalapp answered with an error; try again in a few minutes."

PRICE_DESCRIPTION = (
    "What a grocery costs.\n"
    "The latest price per chain from Kassalapp, with the day it was checked. "
    "'product' is the words the person said, such as 'lettmelk' or 'Tine lettmelk 1 liter', "
    "or the name or barcode ('ean') of a choice the last answer offered. "
    "'chain' is a chain's name, such as 'Meny' or 'Joker'; omit it for every chain."
)

CHEAPEST_DESCRIPTION = (
    "Which chain is cheapest?\n"
    "Ranks the chains by their current price from Kassalapp; a price unchecked for 14 days never counts. "
    "'product' is the words the person said, such as 'kaffe', "
    "or the name or barcode ('ean') of a choice the last answer offered."
)


def build_server(
    answers: PriceAnswers, *, simulated: bool = False, answer_within: float = ANSWER_WITHIN
) -> MCPServer[Any]:
    """The MCP server with the price tools, answering from ``answers`` within ``answer_within`` seconds."""
    server: MCPServer[Any] = MCPServer(
        name="kassalapp-simulated" if simulated else "kassalapp",
        instructions=(SIMULATED + INSTRUCTIONS) if simulated else INSTRUCTIONS,
    )

    @server.tool(annotations=ToolAnnotations(read_only_hint=True), description=PRICE_DESCRIPTION)
    @_guarded(simulated=simulated, answer_within=answer_within)
    def grocery_price(product: str, chain: str | None = None) -> dict[str, Any]:
        return answers.grocery_price(product, chain)

    @server.tool(annotations=ToolAnnotations(read_only_hint=True), description=CHEAPEST_DESCRIPTION)
    @_guarded(simulated=simulated, answer_within=answer_within)
    def grocery_cheapest(product: str) -> dict[str, Any]:
        return answers.grocery_cheapest(product)

    return server


def _guarded(
    *, simulated: bool, answer_within: float
) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]:
    """Make a tool's failure an answer the voice can still read."""

    def wrap(tool: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        @functools.wraps(tool)
        def answered(*args: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
            work = _WORKERS.submit(tool, *args, **kwargs)
            try:
                # RULE: a question answers within one deadline even when kassalapp is slow
                answer = work.result(timeout=answer_within)
            except concurrent.futures.TimeoutError:
                log.warning("%s: no answer within %.0f s; it carries on", tool.__name__, answer_within)
                answer = {"success": False, "outcome": "slow", "say": SLOW}
            # RULE: every tool answers with a sentence even when it fails
            except httpx.TransportError as failure:
                log.warning("%s: Kassalapp did not answer (%s)", tool.__name__, type(failure).__name__)
                answer = {"success": False, "outcome": "unreachable", "say": DROPPED}
            # RULE: an error from kassalapp is put on kassalapp
            except httpx.HTTPStatusError as failure:
                # The path only: the query holds the search words, which are the user's shopping.
                status, path = failure.response.status_code, failure.request.url.path
                log.warning("%s: Kassalapp answered %d on %s", tool.__name__, status, path)
                answer = {"success": False, "outcome": "kassalapp_error", "say": KASSALAPP_ERROR}
            except Exception as failure:  # noqa: BLE001 - whatever broke, the voice gets a sentence
                # The type and where it broke, never the message: it may quote the search words.
                where = "".join(traceback.format_tb(failure.__traceback__))
                log.error("%s broke: %s\n%s", tool.__name__, type(failure).__name__, where)  # noqa: TRY400
                answer = {"success": False, "outcome": "broke", "say": BROKE}
            answer["simulated"] = simulated
            if simulated:
                answer["say"] = "Simulated: " + answer["say"]
            return answer

        return answered

    return wrap
