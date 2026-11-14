"""A cold first start shows the voice its tools within the bridge's start limit.

The bridge starts a plugin as a fresh process and gives up if the tools are not
listed in time, and the voice then sees nothing. "Cold" here means a new
interpreter with no compiled bytecode to reuse, so every heavy import is paid
in full, as on the first start after an update.
"""

import asyncio
import os
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

#: claude-voice's ``plugins.START_SECONDS``, read there on 2026-11-14. The bridge owns
#: the figure; it is copied here because the bridge is not a dependency of this package.
BRIDGE_START_SECONDS = 15.0


async def _seconds_until_tools_listed(env):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "kassalapp_mcp.cli", "serve", "--simulate"], env=env
    )
    started = time.monotonic()
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        took = time.monotonic() - started
    return took, sorted(t.name for t in tools.tools)


def test_a_cold_first_start_lists_the_tools_within_the_bridges_start_limit(tmp_path):
    env = dict(os.environ)
    # An empty bytecode folder: nothing compiled before can be reused.
    env["PYTHONPYCACHEPREFIX"] = str(tmp_path / "pycache")
    took, names = asyncio.run(_seconds_until_tools_listed(env))
    assert names == ["grocery_cheapest", "grocery_price"]
    assert took < BRIDGE_START_SECONDS, f"a cold start took {took:.1f} s"
