// Home: the drop, composed as a poster. The title and the countdown, then the prize, the pool
// and the seats as blocks, then the one thing to do.

import { h, fill, stateStrip, countdown, money, timeOfDay } from "../dom.js";
import { prizeBlock, disclosure, ledgerTail, seatRings, authorityBar } from "../components.js";

export function mount(ctx) {
  const el = h("div", { class: "stack-lg" });
  const paint = (c) => fill(el, build(c));
  paint(ctx);
  return { el, update: paint };
}

function build(ctx) {
  const { state, me } = ctx;
  const g = state.group;
  const pool = state.pool || {};
  const players = state.players || [];
  const cancelled = g.status === "CANCELLED" || g.status === "REFUNDING";
  const full = players.length >= g.max_players;
  const atCapacity = pool.at_capacity || (state.drop && state.drop.pool_at_capacity) || {};
  const potential = Number(atCapacity.gross) || Number(g.entry_amount) * g.max_players;
  const note = state.drop && state.drop.note;

  return [
    h(
      "section",
      { class: "poster", "aria-label": "The drop" },
      h(
        "div",
        { class: "p-title" },
        h("h1", null, g.title),
        h(
          "p",
          { class: "lead" },
          `Three players each set a personal goal with the coach and prove it as they go. The most verified points wins, and the agent buys the prize from the pool.`,
        ),
      ),
      clockBlock(state, cancelled),
      h("div", { class: "block paper p-prize" }, prizeBlock(state), note ? h("p", { class: "tiny muted", style: { marginTop: "10px" } }, note) : null),
      h(
        "div",
        { class: "block wheat p-pool" },
        h("div", { class: "label" }, "The pool"),
        h("div", { class: "numeral" }, money(pool.gross, "")),
        h("p", { class: "small" }, `test USDC from ${players.length} of ${g.max_players} entries of ${money(g.entry_amount, "")}. Full, it holds ${money(potential, "")}.`),
        h("div", { style: { marginTop: "18px" } }, authorityBar(state)),
      ),
      h(
        "div",
        { class: "block night p-seats stack" },
        h("div", { class: "label" }, `${players.length} of ${g.max_players} seats taken`),
        seatRings(state, ctx.meId),
        me ? h("p", { class: "small muted" }, `You hold seat ${me.seat}.`) : h("p", { class: "small muted" }, g.starts_at ? `It starts at ${timeOfDay(g.starts_at)} if all ${g.max_players} seats are taken and everyone has accepted; otherwise every entry is refunded.` : `It starts when all ${g.max_players} seats are taken and everyone accepts.`),
      ),
      h("div", { class: "p-cta" }, callToAction(ctx, full, cancelled)),
    ),
    cancelled
      ? h("p", { class: "banner error", role: "status" }, h("b", null, "This group was cancelled."), " Every entry is refunded; the refunds are on the ledger below.")
      : stateStrip(g.status),
    h("section", { class: "plain stack" }, h("h2", null, "How the money works"), disclosure(state)),
    cancelled ? ledgerTail(state) : null,
  ];
}

function clockBlock(state, cancelled) {
  const g = state.group;
  if (cancelled) {
    return h("div", { class: "block night p-clock" }, h("div", { class: "label" }, "Cancelled"), h("div", { class: "countdown" }, "Refunded"));
  }
  const open = g.status === "OPEN_FOR_JOINING";
  const live = g.status === "ACTIVE";
  const before = ["OPEN_FOR_JOINING", "INTAKE", "READY_FOR_ACCEPTANCE"].includes(g.status);
  const startAt = g.starts_at || g.enrolment_deadline;
  const label = before && g.starts_at ? "Starts in" : open ? "Enrolment closes in" : live ? "Submissions close in" : "The challenge";
  return h(
    "div",
    { class: "block field p-clock" },
    h("div", { class: "label" }, label),
    (before && g.starts_at) || open || live
      ? countdown(live ? g.ends_at : startAt, { done: live ? "Closed" : "Starting" })
      : h("div", { class: "countdown" }, `${g.duration_days} days`),
    state.clock ? h("span", { class: "pill accent" }, state.clock.label) : null,
  );
}

function callToAction(ctx, full, cancelled) {
  const { state, me, go } = ctx;
  const g = state.group;
  if (cancelled) return h("p", { class: "muted" }, "Reset the demo group at the foot of the page to open a new drop.");
  if (me) return h("button", { class: "btn primary block", type: "button", onclick: () => go(stageFor(g.status)) }, "Go to my seat");
  if (g.status === "OPEN_FOR_JOINING" && !full) {
    return h("button", { class: "btn primary block", type: "button", onclick: () => go("join") }, `Join for ${money(g.entry_amount)}`);
  }
  return h("button", { class: "btn ghost block", type: "button", onclick: () => go(stageFor(g.status)) }, "The seats are taken. Watch the room");
}

function stageFor(status) {
  return { OPEN_FOR_JOINING: "join", INTAKE: "coach", READY_FOR_ACCEPTANCE: "agreement", ACTIVE: "challenge" }[status] || "result";
}
