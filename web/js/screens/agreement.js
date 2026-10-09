// Group agreement: every contract, the rubric and the terms; each player accepts or declines.

import { h, fill, act, errorLine, money, pill, plural } from "../dom.js";
import { api } from "../api.js";
import { contractCard, rubricCard, disclosure } from "../components.js";

export function mount(ctx) {
  const el = h("div", { class: "stack-lg" });
  const err = errorLine();
  let busy = false;
  const paint = (c) => {
    if (busy) return;
    fill(el, build(c, err, (b) => (busy = b)));
  };
  paint(ctx);
  return { el, update: paint };
}

function build(ctx, err, setBusy) {
  const { state, me } = ctx;
  const g = state.group;
  const players = state.players || [];
  const accepted = players.filter((p) => p.accepted).length;
  const open = g.status === "READY_FOR_ACCEPTANCE";

  return [
    h(
      "section",
      { class: "stack" },
      h("div", { class: "eyebrow" }, "Group agreement"),
      h("h1", null, "Everyone sees every goal before it starts"),
      h(
        "p",
        { class: "lead" },
        open
          ? `${accepted} of ${players.length} accepted. The challenge starts the moment the last player accepts.`
          : g.status === "INTAKE"
            ? "The agreement opens once every contract is locked."
            : `${accepted} of ${players.length} accepted. The terms below are locked.`,
      ),
    ),
    me && open ? myDecision(ctx, err, setBusy) : null,
    h(
      "section",
      { class: "seats" },
      players.map((p) =>
        h(
          "div",
          { class: "stack" },
          h("div", { class: "row between" }, h("h3", null, p.name), p.accepted ? pill("ACCEPTED") : pill("PENDING", "Not yet accepted")),
          contractCard(p.contract, { hideStatus: true }),
        ),
      ),
    ),
    h(
      "div",
      { class: "grid halves" },
      terms(state),
      rubricCard(state.rubric),
    ),
  ];
}

function myDecision(ctx, err, setBusy) {
  const { me } = ctx;
  if (me.accepted) {
    const players = ctx.state.players || [];
    const waiting = players.filter((p) => !p.accepted).map((p) => p.name);
    return h(
      "section",
      { class: "card stack" },
      h("div", { class: "row" }, pill("ACCEPTED", "You accepted")),
      h("p", { class: "lead" }, waiting.length ? `Waiting for ${waiting.join(" and ")}.` : "Everyone has accepted."),
    );
  }
  const accept = h("button", { class: "btn primary", type: "button" }, "I accept these terms");
  const decline = h("button", { class: "btn danger", type: "button" }, "Decline and cancel");
  const run = async (button, label, fn) => {
    setBusy(true);
    const res = await act(button, err, label, fn);
    setBusy(false);
    if (res && res.state) ctx.setState(res.state);
    else ctx.refresh();
  };
  accept.addEventListener("click", () => run(accept, "Accepting", () => api.accept(ctx.meId)));
  decline.addEventListener("click", () => {
    if (!confirm("Declining cancels the group for everyone and refunds every entry. Decline?")) return;
    run(decline, "Declining", () => api.decline(ctx.meId));
  });
  return h(
    "section",
    { class: "card stack" },
    h("h2", null, `${me.name}, do you accept?`),
    h("p", null, "You accept your own contract, every other player's contract, the rubric and the terms. A decline cancels the group and refunds every entry."),
    err,
    h("div", { class: "row" }, accept, decline),
  );
}

function terms(state) {
  const g = state.group;
  const pool = state.pool || {};
  const prize = state.prize || {};
  const rubric = state.rubric || {};
  const rows = [
    ["Entry", money(g.entry_amount)],
    ["Pool", `${money(pool.gross)} from ${plural(pool.entries || 0, "entry", "entries")}`],
    ["Prize", prize.name || "-"],
    ["Landed quote", prize.quote ? money(prize.quote.final_amount, "USD") : "Quoted at the end"],
    ["Agent's spending ceiling", money(pool.ceiling)],
    ["Duration", `${g.duration_days} days${state.clock ? `, ${state.clock.label.replace(/^Demo time: /, "demo time ")}` : ""}`],
    ["Tie-break", rubric.tie_break || "-"],
    ["Disputes", "A short window after the deadline; a disputed score is held for review before the winner is named."],
  ];
  return h(
    "section",
    { class: "card stack" },
    h("h2", null, "The terms"),
    h("ul", { class: "list terms" }, rows.map(([k, v]) => h("li", null, h("span", { class: "muted" }, k), h("span", { class: "v" }, v)))),
    disclosure(state),
  );
}
