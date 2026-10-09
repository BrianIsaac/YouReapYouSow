// Result: frozen standings, the dispute window, the winner, the prize purchase and the ledger.

import { h, fill, act, errorLine, showError, pill, countdown, money, msUntil, timeOfDay } from "../dom.js";
import { api } from "../api.js";
import { standingsTable, ledgerTail } from "../components.js";
import { eventRow } from "./challenge.js";

const STEPS = [
  ["search", "Search Reap's catalogue for the prize"],
  ["details", "Read the item's details"],
  ["variant", "Pick the variant"],
  ["quote", "Get a landed quote, shipped to Singapore"],
  ["gate", "Check the quote against the pool's ceiling"],
  ["checkout", "Check out under an idempotency key"],
  ["reading", "Read the order until it has an id"],
  ["done", "Order placed"],
];

const BACKEND_LABEL = {
  sandbox: "Reap sandbox, Agentic module",
  kwal: "Kwal",
  mock: "Local mock of Reap, not the real service",
};

export function mount(ctx) {
  const el = h("div", { class: "stack-lg" });
  const err = errorLine();
  const disputeErr = errorLine();
  let events = [];
  let buying = false;
  let editing = null;
  let ctxNow = ctx;

  async function loadEvents() {
    try {
      const res = await api.events(null);
      events = (res && res.events) || [];
    } catch {
      /* the next poll retries */
    }
    paint(ctxNow);
  }

  async function finalize(button) {
    buying = true;
    paint(ctxNow);
    const res = await act(button, err, "The agent is buying the prize", () => api.finalize());
    buying = false;
    if (res && res.state) ctxNow.setState(res.state);
    else paint(ctxNow);
  }

  function paint(c) {
    ctxNow = c;
    if (el.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;
    fill(el, build(c, { events, buying, err, disputeErr, finalize, editing, setEditing: (id) => { editing = id; paint(ctxNow); }, reload: loadEvents }));
  }

  paint(ctx);
  loadEvents();
  return {
    el,
    update(c) {
      paint(c);
      loadEvents();
    },
  };
}

function build(ctx, ui) {
  const { state } = ctx;
  const g = state.group;
  const result = state.result;
  const pending = g.status === "RESULTS_PENDING" || g.status === "DISPUTE_WINDOW";
  const before = ["OPEN_FOR_JOINING", "INTAKE", "READY_FOR_ACCEPTANCE", "ACTIVE"].includes(g.status);

  if (before) {
    return h(
      "section",
      { class: "stack-lg" },
      h("div", { class: "stack" }, h("div", { class: "eyebrow" }, "Result"), h("h1", null, "No result yet"), h("p", { class: "lead" }, g.status === "ACTIVE" ? "The challenge is still running." : "The challenge has not started.")),
      h("section", { class: "card stack" }, h("h2", null, "Standings"), standingsTable(state, ctx.meId)),
      ledgerTail(state),
    );
  }

  const standings = (result && result.standings) || state.leaderboard;
  return [
    h(
      "section",
      { class: "stack" },
      h("div", { class: "eyebrow" }, "Result"),
      h("h1", null, pending ? "Standings are frozen" : result && result.winner ? `${result.winner.name} wins` : "The result"),
      pending ? h("p", { class: "lead" }, "Submissions are closed. Any player can dispute a score before the window ends; then the winner is named and the agent buys the prize.") : null,
    ),
    result && result.winner ? winnerBlock(state, result) : null,
    h(
      "div",
      { class: "grid two" },
      h(
        "div",
        { class: "stack-lg" },
        pending || (result && result.purchase && result.purchase.status === "FAILED") || ui.buying ? finishBlock(ctx, ui) : null,
        result && result.purchase && !ui.buying ? purchaseBlock(state, result.purchase) : null,
        ui.buying && !(result && result.purchase) ? buyingBlock() : null,
        h("section", { class: "card stack" }, h("div", { class: "row between" }, h("h2", null, "Final standings"), pill("LOCKED", "Frozen")), standingsTable(state, ctx.meId, standings), result && result.tie_break_applied ? h("p", { class: "small muted" }, `Tie-break applied: ${state.rubric ? state.rubric.tie_break : ""}`) : null),
      ),
      h("div", { class: "stack-lg" }, disputesBlock(ctx, ui, pending), ledgerTail(state)),
    ),
  ];
}

function winnerBlock(state, result) {
  return h(
    "section",
    { class: "card winner" },
    h("div", { class: "eyebrow" }, "Winner"),
    h("div", { class: "name" }, result.winner.name),
    h("p", { class: "lead" }, `${result.winner.score} verified points. The prize: ${state.prize ? state.prize.name : "the prize"}.`),
  );
}

function finishBlock(ctx, ui) {
  const g = ctx.state.group;
  const purchase = ctx.state.result && ctx.state.result.purchase;
  const left = msUntil(g.dispute_window_ends_at);
  const windowOpen = g.status === "DISPUTE_WINDOW" && left !== null && left > 0;
  const button = h("button", { class: "btn primary block", type: "button" }, purchase && purchase.status === "FAILED" ? "Try the purchase again" : "Name the winner and buy the prize");
  button.disabled = g.status === "RESULTS_PENDING" || windowOpen || ui.buying;
  button.addEventListener("click", () => ui.finalize(button));
  if (ui.buying) {
    button.disabled = true;
    button.textContent = "The agent is buying the prize";
  }
  return h(
    "section",
    { class: "card stack" },
    h("div", { class: "eyebrow" }, g.status === "RESULTS_PENDING" ? "Results pending" : "Dispute window"),
    g.status === "DISPUTE_WINDOW" && g.dispute_window_ends_at
      ? h("div", { class: "stack" }, countdown(g.dispute_window_ends_at, { done: "The window has closed" }), h("p", { class: "small muted" }, `Closes at ${timeOfDay(g.dispute_window_ends_at)}.`))
      : h("p", { class: "lead" }, "Freezing the standings."),
    ui.err,
    button,
    h("p", { class: "small muted" }, "The agent quotes the prize, checks the landed amount against the pool's ceiling, then checks out. It can take up to a minute."),
  );
}

function buyingBlock() {
  return h(
    "section",
    { class: "card stack" },
    h("div", { class: "row between" }, h("h2", null, "The agent is buying the prize"), pill("BUYING")),
    h("ol", { class: "purchase-steps" }, h("li", { class: "now" }, h("span", { class: "dot" }), h("span", null, "Working through Reap's purchase steps"), h("span"))),
  );
}

function purchaseBlock(state, p) {
  const at = STEPS.findIndex(([id]) => id === p.step);
  const gate = p.gate || {};
  const gateCls = { allow: "verified", refuse: "rejected", escalate: "pending" }[gate.disposition] || "";
  const gateWord = { allow: "Allowed", refuse: "Refused", escalate: "Needs a person" }[gate.disposition] || "Not yet checked";
  return h(
    "section",
    { class: "card stack" },
    h("div", { class: "row between" }, h("h2", null, "The prize purchase"), pill(p.status)),
    h(
      "ol",
      { class: "purchase-steps" },
      STEPS.map(([id, label], i) => {
        let cls = "todo";
        if (p.status === "PURCHASED" || i < at) cls = "done";
        else if (i === at) cls = p.status === "FAILED" ? "failed" : p.status === "BUYING" ? "now" : "done";
        return h("li", { class: cls }, h("span", { class: "dot" }), h("span", null, label), h("span", { class: "tiny muted" }, id === "gate" && gate.disposition ? gateWord : ""));
      }),
    ),
    h(
      "div",
      { class: "card quiet stack" },
      h("div", { class: "row between" }, h("span", { class: "muted" }, "Landed quote"), h("b", { class: "num" }, money(p.quote_final_amount, "USD"))),
      h("div", { class: "row between" }, h("span", { class: "muted" }, "Ceiling, the pool less the buffer"), h("b", { class: "num" }, money(p.ceiling))),
      h("div", { class: "row between" }, h("span", { class: "muted" }, "Authority gate"), h("span", { class: `pill ${gateCls}` }, gateWord)),
      gate.reason ? h("p", { class: "small muted" }, gate.reason) : null,
      p.final_amount ? h("div", { class: "row between" }, h("span", { class: "muted" }, "Charged"), h("b", { class: "num" }, money(p.final_amount, "USD"))) : null,
      h("div", { class: "row between" }, h("span", { class: "muted" }, "Through"), h("span", null, BACKEND_LABEL[p.backend] || p.backend || "-")),
    ),
    p.order_id
      ? h("div", { class: "stack" }, h("div", { class: "eyebrow" }, "Order id"), h("div", { class: "order-id" }, p.order_id), p.checkout_id ? h("div", { class: "tiny muted mono" }, `Checkout ${p.checkout_id}`) : null)
      : null,
    p.error ? h("p", { class: "banner error" }, p.error) : null,
    h("p", { class: "stand-in" }, p.stand_in || (state.pool && state.pool.stand_in) || ""),
  );
}

function disputesBlock(ctx, ui, pending) {
  const { me } = ctx;
  const list = ui.events.slice().sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
  const names = Object.fromEntries((ctx.state.players || []).map((p) => [p.player_id, p.name]));
  return h(
    "section",
    { class: "card stack" },
    h("h2", null, "Check-ins"),
    pending && me ? h("p", { class: "small muted" }, "Think a score is wrong? Dispute it while the window is open; it is held for review.") : null,
    ui.disputeErr,
    list.length
      ? h(
          "ul",
          { class: "list checkins" },
          list.map((e) => {
            const row = eventRow({ ...e }, false);
            row.firstChild.prepend(h("span", { class: "small" }, `${names[e.participant_id] || "A player"}: `));
            if (pending && me && ctx.state.group.status === "DISPUTE_WINDOW" && e.state !== "DISPUTED") {
              row.append(ui.editing === e.event_id ? disputeForm(ctx, ui, e) : h("div", { class: "why-line" }, h("button", { class: "btn ghost", type: "button", style: { minHeight: "40px", padding: "0 14px" }, onclick: () => ui.setEditing(e.event_id) }, "Dispute this score")));
            }
            return row;
          }),
        )
      : h("p", { class: "muted" }, "No check-ins were made."),
  );
}

function disputeForm(ctx, ui, e) {
  const reason = h("input", { type: "text", maxlength: "200", placeholder: "Why this score is wrong", "aria-label": "Reason for the dispute" });
  const send = h("button", { class: "btn danger", type: "submit", style: { minHeight: "44px" } }, "Send the dispute");
  const form = h("form", { class: "why-line row" }, h("div", { style: { flex: "1", minWidth: "180px" } }, reason), send);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!reason.value.trim()) return showError(ui.disputeErr, new Error("Give a reason for the dispute."));
    const res = await act(send, ui.disputeErr, "Sending", () => api.dispute(ctx.meId, e.event_id, reason.value.trim()));
    if (res) {
      reason.blur();
      ui.setEditing(null);
      ui.reload();
    }
  });
  queueMicrotask(() => reason.focus());
  return form;
}
