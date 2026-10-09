// Home: the drop. The prize, the entry, the pool, the spots and the enrolment countdown.

import { h, fill, stateStrip, pill } from "../dom.js";
import { prizeBlock, poolFigures, disclosure, deadlineBlock, ledgerTail } from "../components.js";

export function mount(ctx) {
  const el = h("div", { class: "stack-lg" });
  const paint = (c) => fill(el, build(c));
  paint(ctx);
  return { el, update: paint };
}

function build(ctx) {
  const { state, me } = ctx;
  const g = state.group;
  const cancelled = g.status === "CANCELLED" || g.status === "REFUNDING";
  const players = state.players || [];
  const full = players.length >= g.max_players;

  return [
    h(
      "section",
      { class: "stack" },
      h("div", { class: "eyebrow" }, "Tonight's drop"),
      h("h1", null, g.title),
      h(
        "p",
        { class: "lead" },
        `${g.max_players} people, ${g.max_players} personal goals, one prize. Each player agrees a goal with the coach, everyone accepts the same rules, and whoever earns the most verified points wins. The agent then buys the prize for them through Reap.`,
      ),
      cancelled ? null : stateStrip(g.status),
    ),
    cancelled
      ? h(
          "p",
          { class: "banner error", role: "status" },
          h("b", null, g.status === "REFUNDING" ? "Refunding." : "This group was cancelled."),
          " Every entry is refunded to its player; the refunds are on the ledger below.",
        )
      : null,
    h(
      "div",
      { class: "grid two" },
      h("section", { class: "card" }, prizeBlock(state)),
      h(
        "section",
        { class: "card" },
        h(
          "div",
          { class: "stack" },
          g.status === "OPEN_FOR_JOINING"
            ? deadlineBlock("Enrolment closes in", g.enrolment_deadline, "Enrolment closed", state)
            : h("div", { class: "stack" }, h("div", { class: "eyebrow" }, "Challenge"), h("p", { class: "big" }, `${g.duration_days} days`), state.clock ? h("span", { class: "pill accent" }, state.clock.label) : null),
          callToAction(ctx, full, cancelled),
        ),
      ),
    ),
    h("section", { class: "card" }, poolFigures(state)),
    h(
      "div",
      { class: "grid halves" },
      h(
        "section",
        { class: "card" },
        h(
          "div",
          { class: "stack" },
          h("h2", null, "Seats"),
          h(
            "ul",
            { class: "list" },
            Array.from({ length: g.max_players }, (_, i) => {
              const p = players.find((x) => x.seat === i + 1);
              return h(
                "li",
                { class: "row between" },
                h("span", null, h("span", { class: "muted" }, `Seat ${i + 1}  `), p ? h("b", null, p.name) : h("span", { class: "muted" }, "Open")),
                p ? pill(p.entry) : null,
              );
            }),
          ),
          me ? h("p", { class: "small muted" }, `You hold seat ${me.seat} as ${me.name}.`) : null,
        ),
      ),
      h("section", { class: "card" }, h("div", { class: "stack" }, h("h2", null, "How the money works"), disclosure(state))),
    ),
    cancelled ? ledgerTail(state) : null,
  ];
}

function callToAction(ctx, full, cancelled) {
  const { state, me, go } = ctx;
  const g = state.group;
  if (cancelled) return h("p", { class: "muted" }, "Reset the demo group to open a new drop.");
  if (me) {
    return h("button", { class: "btn primary block", type: "button", onclick: () => go(stageFor(g.status)) }, "Go to my seat");
  }
  if (g.status === "OPEN_FOR_JOINING" && !full) {
    return h(
      "div",
      { class: "stack" },
      h("button", { class: "btn primary block", type: "button", onclick: () => go("join") }, `Join for ${Number(g.entry_amount).toFixed(2)} test USDC`),
      h("p", { class: "small muted" }, "Refunded in full if the group does not start."),
    );
  }
  return h(
    "div",
    { class: "stack" },
    h("p", null, "This drop is full. You can watch the room."),
    h("button", { class: "btn ghost block", type: "button", onclick: () => go(stageFor(g.status)) }, "Watch the room"),
  );
}

function stageFor(status) {
  return (
    {
      OPEN_FOR_JOINING: "join",
      INTAKE: "coach",
      READY_FOR_ACCEPTANCE: "agreement",
      ACTIVE: "challenge",
    }[status] || "result"
  );
}
