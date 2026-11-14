"""The MCP server: the price tool, over stdio, for a voice session."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

if TYPE_CHECKING:
    from collections.abc import Callable

    from kassalapp_mcp.answers import PriceAnswers

#: The Claude app shows about this many characters of a tool before loading it.
FIRST_LINE_MAX = 26
INSTRUCTIONS = (
    "Grocery prices per chain from Kassalapp. Read each 'say' aloud as it is. "
    "When the answer asks which product, read the choices and wait; never pick one. "
    "It only reads prices and never buys anything."
)
SIMULATED = "SIMULATED prices, not real ones: say so. "
#: What the voice hears when Kassalapp cannot be reached.
DROPPED = "Kassalapp did not answer; try again in a minute."
#: What the voice hears when the plugin itself fails, so it never hears only that the tool failed.
BROKE = "That went wrong inside kassalapp-mcp. Try again, and if it fails again, check its log."

PRICE_DESCRIPTION = (
    "What a grocery costs.\n"
    "The latest price per chain from Kassalapp, with the day it was checked. "
    "'product' is the words the person said, such as 'lettmelk' or 'Tine lettmelk 1 liter'. "
    "'chain' is a chain's name, such as 'Meny' or 'Joker'; omit it for every chain."
)

CHEAPEST_DESCRIPTION = (
    "Which chain is cheapest?\n"
    "Ranks the chains by their current price from Kassalapp; a price unchecked for 14 days never counts. "
    "'product' is the words the person said, such as 'kaffe' or 'Tine lettmelk 1 liter'."
)


def build_server(answers: PriceAnswers, *, simulated: bool = False) -> MCPServer[Any]:
    """The MCP server with the price tool, answering from ``answers``."""
    server: MCPServer[Any] = MCPServer(
        name="kassalapp-simulated" if simulated else "kassalapp",
        instructions=(SIMULATED + INSTRUCTIONS) if simulated else INSTRUCTIONS,
    )

    @server.tool(annotations=ToolAnnotations(read_only_hint=True), description=PRICE_DESCRIPTION)
    @_guarded(simulated=simulated)
    def grocery_price(product: str, chain: str | None = None) -> dict[str, Any]:
        return answers.grocery_price(product, chain)

    @server.tool(annotations=ToolAnnotations(read_only_hint=True), description=CHEAPEST_DESCRIPTION)
    @_guarded(simulated=simulated)
    def grocery_cheapest(product: str) -> dict[str, Any]:
        return answers.grocery_cheapest(product)

    return server


def _guarded(*, simulated: bool) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]:
    """Make a tool's failure an answer the voice can still read."""

    def wrap(tool: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        @functools.wraps(tool)
        def answered(*args: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
            try:
                answer = tool(*args, **kwargs)
            # RULE: every tool answers with a sentence even when it fails
            except httpx.TransportError:
                answer = {"success": False, "outcome": "unreachable", "say": DROPPED}
            except Exception:  # noqa: BLE001 - whatever broke, the voice gets a sentence
                answer = {"success": False, "outcome": "broke", "say": BROKE}
            answer["simulated"] = simulated
            if simulated:
                answer["say"] = "Simulated: " + answer["say"]
            return answer

        return answered

    return wrap
