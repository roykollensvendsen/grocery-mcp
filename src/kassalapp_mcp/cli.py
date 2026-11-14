"""Command line for kassalapp-mcp: serve the price tool, or ask one price."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import pathlib
import sys

import httpx

from kassalapp_mcp.answers import PriceAnswers
from kassalapp_mcp.fake import FAKE_KEY, FakeKassalapp
from kassalapp_mcp.source import BASE_URL, KassalappSource

#: Every verb this command has. The documentation test requires each one to
#: appear in a worked example, so a verb cannot be added without one.
COMMANDS: tuple[str, ...] = ("serve", "price")
#: Where the user's own Kassalapp key is kept, readable by them alone.
KEY_FILE = pathlib.Path("~/.config/kassalapp-mcp/key")


def _answers(*, simulate: bool, key_file: str | None) -> PriceAnswers:
    if simulate:
        source = KassalappSource(FakeKassalapp().client(), lambda: FAKE_KEY)
    else:
        key = pathlib.Path(key_file).expanduser() if key_file else KEY_FILE.expanduser()
        source = KassalappSource(httpx.Client(base_url=BASE_URL), key)
    return PriceAnswers(source, today=dt.date.today)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch. Returns a process exit code."""
    parser = argparse.ArgumentParser(prog="kassalapp-mcp", description=__doc__)
    verbs = parser.add_subparsers(dest="command", required=True)
    serve = verbs.add_parser("serve", help="serve the price tool over stdio, for a voice session")
    price = verbs.add_parser("price", help="ask one price, and print what the voice would hear")
    price.add_argument("product", help="the words for the product, such as 'lettmelk'")
    price.add_argument("--chain", help="one chain, such as Meny; leave it out for every chain")
    for verb in (serve, price):
        verb.add_argument("--simulate", action="store_true", help="answer from made-up prices")
        verb.add_argument("--key-file", help=f"the Kassalapp key (default {KEY_FILE})")
    args = parser.parse_args(argv)

    # The MCP server is a slow import, and only a working command needs it.
    from mcp.types import CallToolResult  # noqa: PLC0415

    from kassalapp_mcp.server import build_server  # noqa: PLC0415

    server = build_server(_answers(simulate=args.simulate, key_file=args.key_file), simulated=args.simulate)
    if args.command == "price":
        asked = {"product": args.product, "chain": args.chain}
        result = asyncio.run(server.call_tool("grocery_price", asked))
        answer = result.structured_content if isinstance(result, CallToolResult) else None
        sys.stdout.write(str((answer or {}).get("say", "")) + "\n")
        return 0
    server.run("stdio")  # pragma: no cover - runs until stdin closes
    return 0  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
