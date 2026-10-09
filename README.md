# YouReapYouSow

Compete on your own goals, stay accountable together, and earn the item your group is playing for.

## Run it

You need [uv](https://docs.astral.sh/uv/); Python 3.12 is pinned and installed by uv.

```bash
cp .env.example .env        # then fill in what you use; the mock needs nothing
uv sync
uv run uvicorn youreapyousow.api.app:serve --factory --port 8000
```

The gate, run before every commit:

```bash
uv run pytest && uv run ruff check && uv run ruff format --check && uv run pyright
```

## What is inside

- `ledger/`: an append-only, hash-chained ledger in SQLite; every money and score event lands on it.
- `authority/`: the authority gate, seventeen ordered pure rules that decide every purchase before money moves, and claim it exactly once.
- `reap/`: the client for Reap's Agentic module (search, details, variant, quote, checkout) and an in-process mock of it.
- `kwal/`: the same purchase path through the Kwal participant gateway, paid from a vault of test USDC.
- `featherless.py`: chat completions over an OpenAI-compatible endpoint.

## Licence

Apache-2.0, see [LICENSE](LICENSE).
