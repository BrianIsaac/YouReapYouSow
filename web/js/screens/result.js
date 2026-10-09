// Result: frozen standings, the dispute window, the winner, the prize purchase and the ledger.

import { h, fill, act, errorLine, showError, pill, countdown, money, msUntil, timeOfDay } from "../dom.js";
import { api } from "../api.js";
import { standingsTable, ledgerTail, playersBand, authorityBar } from "../components.js";
import { qrSvg } from "../qr.js";

// The purchase step last drawn as done, so newly done steps light up in sequence.
let shownStep = -1;
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

// Shown only for a checkout that went through Reap's hosted approval page.
const APPROVAL_STEP = ["approval", "The card holder approves the charge"];

function stepsFor(p) {
  if (!p.approval_expires_at && !p.approved_at && p.step !== "approval") return STEPS;
  const at = STEPS.findIndex(([id]) => id === "checkout") + 1;
  return [...STEPS.slice(0, at), APPROVAL_STEP, ...STEPS.slice(at)];
}

// A purchase whose approval page closed unused can be reopened with a fresh checkout.
function approvalLapsed(p) {
  return Boolean(p && p.status === "FAILED" && p.approval_expires_at && !p.order_id);
}

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

  async function retry(button) {
    buying = true;
    paint(ctxNow);
    const res = await act(button, err, "Opening a fresh checkout", () => api.retryPurchase());
    buying = false;
    if (res && res.state) ctxNow.setState(res.state);
    else paint(ctxNow);
  }

  function paint(c) {
    ctxNow = c;
    if (el.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;
    fill(el, build(c, { events, buying, err, disputeErr, finalize, retry, editing, setEditing: (id) => { editing = id; paint(ctxNow); }, reload: loadEvents }));
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
      h("h1", null, pending ? "Standings are frozen" : g.status === "FULFILLED" ? "The prize is bought" : result && result.winner ? "The winner is named" : "The result"),
      pending ? h("p", { class: "lead" }, "Submissions are closed. Any player can dispute a score before the window ends; then the winner is named and the agent buys the prize.") : null,
    ),
    result && result.winner ? winnerBlock(state, result) : null,
    h("section", { class: "card", "aria-label": "Every player's final points" }, playersBand(state, ctx.meId, { winnerId: result && result.winner ? result.winner.player_id : null })),
    h(
      "div",
      { class: "grid two" },
      h(
        "div",
        { class: "stack-lg" },
        result && approvalLapsed(result.purchase) ? retryBlock(ui) : pending || (result && result.purchase && result.purchase.status === "FAILED") || ui.buying ? finishBlock(ctx, ui) : null,
        result && result.purchase && result.purchase.status === "AWAITING_APPROVAL" ? approvalBlock(state, result) : null,
        result && result.purchase ? purchaseBlock(state, result.purchase) : null,
        ui.buying && !(result && result.purchase) ? buyingBlock() : null,
        h("section", { class: "card stack" }, h("div", { class: "row between" }, h("h2", null, "Final standings"), pill("LOCKED", "Frozen")), standingsTable(state, ctx.meId, standings), result && result.tie_break_applied ? h("p", { class: "small muted" }, `Tie-break applied: ${state.rubric ? state.rubric.tie_break : ""}`) : null),
      ),
      h("div", { class: "stack-lg" }, disputesBlock(ctx, ui, pending), ledgerTail(state)),
    ),
  ];
}

// The winner's name rises in once, not on every repaint.
let risenFor = null;

function winnerBlock(state, result) {
  const rise = risenFor !== result.winner.player_id;
  risenFor = result.winner.player_id;
  return h(
    "section",
    { class: "winner", "aria-label": "Winner" },
    h(
      "div",
      null,
      h("div", { class: `name ${rise ? "rise" : ""}`.trim() }, result.winner.name),
      h("p", { class: "what" }, `wins the ${state.prize ? state.prize.name : "prize"}, bought by the agent from the pool.`),
    ),
    h("div", { class: "score-line" }, result.winner.score, h("small", null, "verified points")),
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

// The card holder's one tap: the hosted approval page as a code to scan, and its expiry.
function approvalBlock(state, result) {
  const p = result.purchase;
  const prize = state.prize ? state.prize.name : "prize";
  const winner = result.winner ? result.winner.name : "the winner";
  const where = p.backend === "sandbox" ? "Reap's sandbox" : "the local mock of Reap";
  return h(
    "section",
    { class: "card stack approval", "aria-label": "Approve the charge" },
    h("div", { class: "row between" }, h("div", { class: "eyebrow" }, "Waiting for the card holder"), pill("AWAITING_APPROVAL")),
    h("h2", null, `The agent has bought the ${prize} for ${winner}. Approve the charge on your phone.`),
    h(
      "div",
      { class: "approval-body" },
      p.approval_url ? h("a", { class: "qr-frame", href: p.approval_url, target: "_blank", rel: "noopener" }, qrSvg(p.approval_url, "Code for Reap's approval page")) : null,
      h(
        "div",
        { class: "stack" },
        h("p", { class: "small muted" }, "Scan the code with the card holder's phone and confirm with its passkey. The order lands here on its own."),
        h("div", { class: "stack" }, h("div", { class: "label" }, "The page closes in"), countdown(p.approval_expires_at, { done: "The page has closed" }), p.approval_expires_at ? h("p", { class: "tiny muted" }, `At ${timeOfDay(p.approval_expires_at)}.`) : null),
        p.approval_url ? h("a", { class: "btn ghost", href: p.approval_url, target: "_blank", rel: "noopener" }, "Open the approval page") : null,
      ),
    ),
    h("p", { class: "stand-in" }, `A test charge on ${where}, not a real one; the pool is test USDC.`),
  );
}

function retryBlock(ui) {
  const button = h("button", { class: "btn primary block", type: "button" }, "Open a fresh checkout");
  button.addEventListener("click", () => ui.retry(button));
  if (ui.buying) {
    button.disabled = true;
    button.textContent = "Opening a fresh checkout";
  }
  return h(
    "section",
    { class: "card stack" },
    h("div", { class: "eyebrow" }, "The approval page closed"),
    h("p", { class: "lead" }, "The card holder did not approve the charge in time, so nothing was bought."),
    ui.err,
    button,
    h("p", { class: "small muted" }, "The agent quotes the prize again, checks it against the pool's ceiling, and opens a new approval page."),
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
  const steps = stepsFor(p);
  const at = steps.findIndex(([id]) => id === p.step);
  const doneUpTo = p.status === "PURCHASED" ? steps.length - 1 : at - 1;
  const firstNew = shownStep;
  shownStep = Math.max(shownStep, doneUpTo);
  const gate = p.gate || {};
  const gateCls = { allow: "verified", refuse: "rejected", escalate: "pending" }[gate.disposition] || "";
  const gateWord = { allow: "Allowed", refuse: "Refused", escalate: "Needs a person" }[gate.disposition] || "Not yet checked";
  return h(
    "section",
    { class: "card stack" },
    h("div", { class: "row between" }, h("h2", null, "The agent buys the prize"), pill(p.status)),
    h(
      "ol",
      { class: "purchase-steps" },
      steps.map(([id, label], i) => {
        let cls = "todo";
        if (p.status === "PURCHASED" || i < at) cls = "done";
        else if (i === at) cls = p.status === "FAILED" ? "failed" : p.status === "BUYING" || p.status === "AWAITING_APPROVAL" ? "now" : "done";
        const arriving = cls === "done" && i > firstNew;
        if (arriving) cls += " arrive";
        return h("li", { class: cls, style: arriving ? { "--delay": `${(i - firstNew - 1) * 0.22}s` } : null }, h("span", { class: "dot" }), h("span", null, label), h("span", { class: "tiny muted" }, id === "gate" && gate.disposition ? gateWord : ""));
      }),
    ),
    h(
      "div",
      { class: "receipt" },
      h("div", { class: "line" }, h("span", { class: "muted" }, "Landed quote"), h("b", { class: "num" }, money(p.quote_final_amount, "USD"))),
      h("div", { class: "line" }, h("span", { class: "muted" }, "Ceiling, the pool less the buffer"), h("b", { class: "num" }, money(p.ceiling))),
      h("div", { class: "line" }, h("span", { class: "muted" }, "Authority gate"), h("span", { class: `pill ${gateCls}` }, gateWord)),
      gate.reason ? h("div", { class: "line small muted" }, gate.reason) : null,
      p.final_amount ? h("div", { class: "line" }, h("span", { class: "muted" }, "Charged"), h("b", { class: "num" }, money(p.final_amount, "USD"))) : null,
      p.approved_at ? h("div", { class: "line" }, h("span", { class: "muted" }, "Approved by the card holder"), h("b", { class: "num" }, timeOfDay(p.approved_at))) : null,
      h("div", { class: "line" }, h("span", { class: "muted" }, "Through"), h("span", { style: { textAlign: "right" } }, BACKEND_LABEL[p.backend] || p.backend || "-")),
    ),
    h("div", { class: "block soft", style: { padding: "18px" } }, authorityBar(state, p.quote_final_amount)),
    p.order_id
      ? h("div", { class: "order-block stack" }, h("div", { class: "label" }, "Reap order"), h("div", { class: "order-id" }, p.order_id), p.checkout_id ? h("div", { class: "tiny mono" }, `Checkout ${p.checkout_id}`) : null)
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
    pending && me ? h("p", { class: "small muted" }, "Think another player's score is wrong, or yours was rejected unfairly? Dispute it while the window is open; it is held for review.") : null,
    ui.disputeErr,
    list.length
      ? h(
          "ul",
          { class: "list checkins" },
          list.map((e) => {
            const row = eventRow({ ...e }, false);
            row.firstChild.prepend(h("span", { class: "small" }, `${names[e.participant_id] || "A player"}: `));
            const own = e.participant_id === ctx.meId;
            const disputable = e.state !== "DISPUTED" && (own ? e.state === "REJECTED" : e.state === "VERIFIED");
            if (ctx.state.group.status === "DISPUTE_WINDOW" && e.state === "DISPUTED") {
              row.append(reviewButtons(ui, e));
            }
            const windowLeft = msUntil(ctx.state.group.dispute_window_ends_at);
            if (pending && me && ctx.state.group.status === "DISPUTE_WINDOW" && disputable && (windowLeft === null || windowLeft > 0)) {
              row.append(ui.editing === e.event_id ? disputeForm(ctx, ui, e) : h("div", { class: "why-line" }, h("button", { class: "btn ghost", type: "button", style: { minHeight: "40px", padding: "0 14px" }, onclick: () => ui.setEditing(e.event_id) }, "Dispute this score")));
            }
            return row;
          }),
        )
      : h("p", { class: "muted" }, "No check-ins were made."),
  );
}

// The reviewer's verdict on a disputed check-in: tonight, whoever runs the room.
function reviewButtons(ui, e) {
  const keep = h("button", { class: "btn ghost", type: "button", style: { minHeight: "44px", padding: "0 16px" } }, "Reinstate the points");
  const drop = h("button", { class: "btn danger", type: "button", style: { minHeight: "44px", padding: "0 16px" } }, "Reject the check-in");
  const decide = async (button, reinstate) => {
    const res = await act(button, ui.disputeErr, "Recording", () => api.reviewDispute(e.event_id, reinstate));
    if (res) ui.reload();
  };
  keep.addEventListener("click", () => decide(keep, true));
  drop.addEventListener("click", () => decide(drop, false));
  return h("div", { class: "why-line row" }, h("span", { class: "small" }, "Reviewer:"), keep, drop);
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
