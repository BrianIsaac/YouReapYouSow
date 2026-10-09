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

## The drops

`configs/drops.yaml` lists five drops, each with its own prize, entry, three seats and a
date range fixed when it is published (7, 14 or 28 challenge days): the Keychron B40
(featured, USD, bought live on Reap's sandbox), the UGREEN mouse, the Anker Nano hub, the
PRISM+ W290U and a Boxgreen snack bag. Reap's sandbox prices the four Singapore merchants
in SGD; the gate buys only in the pool's currency, USD, so those four are bought on the
local mock of Reap, and each says so. Entries are sized so three clear the landed quote
after a 10% buffer.

Joining, the coach and acceptance happen before a drop's fixed start; at the start the
challenge begins if every seat is taken, every contract locked and everyone has accepted,
otherwise every entry is refunded.

## Running the demo

```bash
uv run uvicorn youreapyousow.api.app:serve --factory --port 8000
# republish the featured drop to start five minutes from now
curl -s -X POST localhost:8000/api/drops/keychron-b40/reset -H 'content-type: application/json' -d '{"lead_s": 300}'
```

Open `http://localhost:8000/`. With `REAP_BACKEND=sandbox` and an ACTIVE
`REAP_ENROLLMENT_ID`, the featured prize is a test charge on the sandbox, never a real one;
while the enrolment is not ACTIVE (the sandbox refuses any checkout without one), the agent
buys on the local mock through the same gate and says so on the result.

On the sandbox the checkout waits on the card holder: Reap answers it with a hosted approval
page (valid 15 minutes, single use) that asks for the passkey on the card holder's own
phone. The purchase reads `AWAITING_APPROVAL`, the result screen shows the page as a QR code
with its countdown, and every poll of the state re-reads the checkout until the order lands
or the page expires. After an expiry, `POST /api/drops/{id}/purchase/retry` opens a fresh
checkout on a fresh quote through the same gate.

## The API

`GET /api/state` is what the room screen polls; the routes under `/api` join, talk to the
coach, edit and lock a contract, accept, check in, dispute, finalise, and reopen a purchase
whose approval page expired.
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
