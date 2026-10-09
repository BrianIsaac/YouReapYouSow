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

## How it works

One drop, one group, three players, one prize. Players join and reserve a 25.00 test USDC
entry each; each talks to the AI coach, which turns their words into a goal contract with
the same 100 points on four milestones (15, 20, 25, 40); everyone sees every contract and
the rubric, and the challenge starts only when all three accept. Check-ins carry a photo,
a clip or a log entry; the rubric decides the points (a vision model's reading of a photo
is shown beside it, advisory). At the end the standings freeze, a short dispute window
passes, the winner is named by the published tie-break, and the agent buys the prize
through Reap's Agentic module behind a deterministic authority gate whose ceiling is the
pool less its buffer. Every money and score event is on the hash-chained ledger.

**Demo time**: `DEMO_CLOCK` sets the real seconds per challenge day; the default makes a
minute stand for a week, and the screen says so.

**The money, labelled**: the pool is test USDC in the Kwal vault on Ink Sepolia, a
labelled stand-in for the card the agent charges. No cash value.

## The API

`GET /api/state` is what the room screen polls; the routes under `/api` join, talk to the
coach, edit and lock a contract, accept, check in, dispute, and finalise.
`GET /api/ledger` and `GET /api/events` read the ledger and the score events back.

## What is inside

- `ledger/`: an append-only, hash-chained ledger in SQLite; every money and score event lands on it.
- `authority/`: the authority gate, seventeen ordered pure rules that decide every purchase before money moves, and claim it exactly once.
- `reap/`: the client for Reap's Agentic module (search, details, variant, quote, checkout) and an in-process mock of it.
- `kwal/`: the same purchase path through the Kwal participant gateway, paid from a vault of test USDC.
- `featherless.py`: chat completions over an OpenAI-compatible endpoint (OpenAI first, Featherless after).
- `game/`: the group's state machine, the coach, the rubric and scoring, the photo reader, and the prize purchase.

## Legal

A pay-to-enter competition with a valuable prize can implicate gambling, lottery or other
contest and promotion laws depending on the jurisdiction, the skill-versus-chance
determination and the design, and stablecoin handling can add payments, digital payment
token, custody, KYC/AML, sanctions, consumer-protection, tax and cross-border obligations;
Singapore is a likely first context but the launch jurisdiction is not confirmed, and
labelling the competition "skill-based" does not settle its status. This prototype
therefore uses test tokens with no cash value and no redeemable prize; local legal review
comes before accepting real stakes or promising prizes. That review has not been done.

## Licence

Apache-2.0, see [LICENSE](LICENSE).
