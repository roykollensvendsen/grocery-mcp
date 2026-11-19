# grocery-mcp

Grocery prices per chain, by voice: an MCP server and claude-voice plugin.
Kassalapp is its first price source.

It was called kassalapp-mcp. The old command still works while the voice
bridge starts it by that name. It stays until the bridge has a
`[plugins.grocery]` entry and the owner has approved it; a later change removes it.
The key, the cache and the private list keep their `kassalapp-mcp` folders, as
they belong to the Kassalapp source.

It answers two questions, by voice: what a grocery costs at each chain, and
where it is cheapest. Every answer says how old the price is. It only reads
prices and never buys anything.

Kassalapp has no current prices from Kiwi, REMA 1000 or Coop, and the plugin
says so rather than give an old price as if it were new. Prices are per chain,
not per shop. Price data comes from [Kassalapp](https://kassal.app).

Try it on made-up prices, with no key and nothing sent:

```console
$ grocery-mcp price "fjellmelk lett 1 l" --chain Meny --simulate
Simulated: Fjellmelk lett 1 l costs 26.90 at Meny, checked today. Kassalapp's prices are per chain, not per shop.
$ grocery-mcp price fjellmelk --simulate
Simulated: Which one: Fjellmelk lett 1 l, Fjellmelk hel 1 l or Fjellmelk skummet 1 l?
```

For real prices, make your own free key at kassal.app/profil/api, and save it
in `~/.config/kassalapp-mcp/key`, readable by you alone. Without it, every
answer says the plugin is not set up, and nothing is sent.

A voice session starts it as an MCP server over stdio:

```console
$ grocery-mcp serve --help
usage: grocery-mcp serve [-h] [--simulate] [--key-file KEY_FILE] [--state-dir STATE_DIR]

options:
  -h, --help            show this help message and exit
  --simulate            answer from made-up prices
  --key-file KEY_FILE   the Kassalapp key (default ~/.config/kassalapp-mcp/key)
  --state-dir STATE_DIR
                        the cache and call counts (default ~/.local/state/kassalapp-mcp)
```

The same question twice within six hours costs one call, not two, because
Kassalapp updates its prices once a day. The cache stays on your machine:
Kassalapp's terms forbid publishing its prices.

The free plan allows 60 calls a minute. The plugin counts its calls and stops
when Kassalapp says the minute's calls are used up. Then it answers from the
cache, or asks you to try again in a minute. Its log of calls holds no search
words and no barcodes. To see the count, here from a folder that has none:

```console
$ grocery-mcp quota --state-dir /nonexistent/grocery-mcp
{
  "limit_per_min": null,
  "remaining": null,
  "seen_at": null,
  "calls_last_min": 0,
  "calls_today": 0,
  "cache_hits_today": 0,
  "429s_today": 0,
  "level": "ok",
  "backing_off_until": null,
  "headers": {}
}
```

## The gates

Every one of these runs in continuous integration, and all of them run here:

```console
$ uv run ruff check .
All checks passed!
```

<!-- not run: the workflow runs each of these; nesting them here would run the suite inside itself -->
```bash
uv run ruff format --check .   # layout
uv run mypy src                # types
uv run pytest -q               # behaviour
uv run python scripts/mutate.py  # that each rule's own test can fail
```

Only one mutation run goes at a time in a checkout. A run that was killed
can leave a source file with a rule switched off. The next run puts it back,
or `uv run python scripts/mutate.py --repair-only` does only that.

`CONTRIBUTING.md` says what each one is for, and
[`decisions/`](decisions/README.md) records the choices that shaped them,
including how a change is made here.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).
