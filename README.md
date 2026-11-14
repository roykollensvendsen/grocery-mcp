# kassalapp-mcp

Grocery prices per chain from Kassalapp, by voice: an MCP server and claude-voice plugin.

It answers one question, by voice: what a grocery costs, per chain, and how
old that price is. It only reads prices and never buys anything.

Kassalapp has no current prices from Kiwi, REMA 1000 or Coop, and the plugin
says so rather than give an old price as if it were new. Prices are per chain,
not per shop. Price data comes from [Kassalapp](https://kassal.app).

Try it on made-up prices, with no key and nothing sent:

```console
$ kassalapp-mcp price "fjellmelk lett" --chain Meny --simulate
Simulated: Fjellmelk lett 1 l costs 26.90 at Meny, checked today. Kassalapp's prices are per chain, not per shop.
$ kassalapp-mcp price fjellmelk --simulate
Simulated: Which one: Fjellmelk lett 1 l, Fjellmelk hel 1 l or Fjellmelk skummet 1 l?
```

For real prices, make your own free key at kassal.app/profil/api, and save it
in `~/.config/kassalapp-mcp/key`, readable by you alone. Without it, every
answer says the plugin is not set up, and nothing is sent.

A voice session starts it as an MCP server over stdio:

```console
$ kassalapp-mcp serve --help
usage: kassalapp-mcp serve [-h] [--simulate] [--key-file KEY_FILE]

options:
  -h, --help           show this help message and exit
  --simulate           answer from made-up prices
  --key-file KEY_FILE  the Kassalapp key (default ~/.config/kassalapp-mcp/key)
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
python scripts/mutate.py       # that each rule's own test can fail
```

`CONTRIBUTING.md` says what each one is for, and
[`decisions/`](decisions/README.md) records the choices that shaped them,
including how a change is made here.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).
