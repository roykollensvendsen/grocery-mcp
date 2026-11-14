"""Command line for kassalapp-mcp: serve the price tool, or ask one price."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import functools
import json
import pathlib
import sys

import httpx

from kassalapp_mcp.answers import PriceAnswers
from kassalapp_mcp.cache import CachedSource
from kassalapp_mcp.fake import FAKE_KEY, FakeKassalapp
from kassalapp_mcp.quota import Quota
from kassalapp_mcp.source import BASE_URL, KassalappSource

#: Every verb this command has. The documentation test requires each one to
#: appear in a worked example, so a verb cannot be added without one.
COMMANDS: tuple[str, ...] = ("serve", "price", "quota")
#: Where the user's own Kassalapp key is kept, readable by them alone.
KEY_FILE = pathlib.Path("~/.config/kassalapp-mcp/key")
#: Where the cache is kept: on the user's machine, outside any repository.
STATE_DIR = pathlib.Path("~/.local/state/kassalapp-mcp")
#: Help is laid out at one width on every terminal, so the README's copy of it stays true.
HELP = functools.partial(argparse.HelpFormatter, width=100)


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _answers(*, simulate: bool, key_file: str | None, state_dir: str | None) -> PriceAnswers:
    if simulate:
        # The simulation keeps its cache and its count in memory, so it leaves nothing behind.
        quota = Quota(None, now=_now)
        fake = KassalappSource(FakeKassalapp().client(), lambda: FAKE_KEY, quota=quota)
        cached = CachedSource(fake, ":memory:", now=_now, on_hit=quota.cache_hit)
        return PriceAnswers(cached, today=dt.date.today)
    key = pathlib.Path(key_file).expanduser() if key_file else KEY_FILE.expanduser()
    state = _state(state_dir)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    quota = Quota(state, now=_now)
    real = KassalappSource(httpx.Client(base_url=BASE_URL), key, quota=quota)
    cached = CachedSource(real, state / "cache.sqlite", now=_now, on_hit=quota.cache_hit)
    return PriceAnswers(cached, today=dt.date.today)


def _state(state_dir: str | None) -> pathlib.Path:
    return pathlib.Path(state_dir).expanduser() if state_dir else STATE_DIR.expanduser()


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch. Returns a process exit code."""
    parser = argparse.ArgumentParser(prog="kassalapp-mcp", description=__doc__, formatter_class=HELP)
    verbs = parser.add_subparsers(dest="command", required=True)
    serve = verbs.add_parser(
        formatter_class=HELP, name="serve", help="serve the price tool over stdio, for a voice session"
    )
    price = verbs.add_parser(
        formatter_class=HELP, name="price", help="ask one price, and print what the voice would hear"
    )
    price.add_argument("product", help="the words for the product, such as 'lettmelk'")
    price.add_argument("--chain", help="one chain, such as Meny; leave it out for every chain")
    quota = verbs.add_parser(
        formatter_class=HELP, name="quota", help="print how many calls to Kassalapp are used and left"
    )
    for verb in (serve, price):
        verb.add_argument("--simulate", action="store_true", help="answer from made-up prices")
        verb.add_argument("--key-file", help=f"the Kassalapp key (default {KEY_FILE})")
    for verb in (serve, price, quota):
        verb.add_argument("--state-dir", help=f"the cache and call counts (default {STATE_DIR})")
    args = parser.parse_args(argv)
    if args.command == "quota":
        sys.stdout.write(json.dumps(Quota(_state(args.state_dir), now=_now).status(), indent=2) + "\n")
        return 0

    # The MCP server is a slow import, and only a working command needs it.
    from mcp.types import CallToolResult  # noqa: PLC0415

    from kassalapp_mcp.server import build_server  # noqa: PLC0415

    answers = _answers(simulate=args.simulate, key_file=args.key_file, state_dir=args.state_dir)
    server = build_server(answers, simulated=args.simulate)
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
