// Pieces more than one screen shows: the prize, the pool, standings, contracts, the ledger.

import { h, money, pill, plural, timeOfDay, countdown, svg, now } from "./dom.js";

export const EVIDENCE_LABEL = {
  photo_or_clip: "A photo or a short clip",
  log: "Your own log entry",
};

export function demoClockNote(state) {
  const label = state.clock && state.clock.label;
  return label ? h("span", { class: "pill accent" }, label) : null;
}

export function prizeBlock(state) {
  const prize = state.prize || {};
  const quote = prize.quote;
  const initial = (prize.merchant || prize.name || "?").trim().charAt(0).toUpperCase();
  const mark = prize.image_url
    ? h("img", { class: "prize-img", src: prize.image_url, alt: prize.name || "The prize" })
    : h("div", { class: "prize-mark", "aria-hidden": "true" }, initial);
  return h(
    "div",
    { class: "stack" },
    h("div", { class: "label" }, "The prize"),
    h("div", { class: "row", style: { alignItems: "center", gap: "20px", flexWrap: "nowrap" } }, mark, h("h2", null, prize.name || "To be announced")),
    h(
      "div",
      { class: "row" },
      quote
        ? h("span", { class: "pill money" }, `Quoted ${money(quote.final_amount, "USD")} landed in Singapore`)
        : h("span", { class: "pill pending" }, "Quoted when the winner is named"),
    ),
    quote
      ? h("p", { class: "small muted" }, `Item ${money(quote.items, "")}, shipping ${money(quote.shipping, "")}, tax ${money(quote.tax, "")}. ${prize.merchant ? `From ${prize.merchant} through Reap.` : ""}`)
      : h("p", { class: "small muted" }, prize.merchant ? `From ${prize.merchant}, bought through Reap by the agent.` : "Bought through Reap by the agent."),
  );
}

export function poolFigures(state) {
  const g = state.group;
  const pool = state.pool || {};
  const filled = (state.players || []).length;
  return h(
    "div",
    { class: "figures" },
    figure("Entry", money(g.entry_amount, ""), "test USDC each"),
    figure("Pool", money(pool.gross, ""), `${plural(pool.entries || 0, "entry", "entries")} in the vault`),
    figure("Spots", `${filled} of ${g.max_players}`, `starts at ${g.min_players}`),
    figure(
      "Prize ceiling",
      money(pool.ceiling, ""),
      pool.buffer ? `pool less a ${money(pool.buffer, "")} buffer` : "pool less the buffer",
    ),
  );
}

function figure(label, value, note) {
  return h(
    "div",
    { class: "figure" },
    h("div", { class: "label" }, label),
    h("div", { class: "value" }, value),
    note ? h("div", { class: "note" }, note) : null,
  );
}

export function disclosure(state) {
  const pool = state.pool || {};
  return h(
    "div",
    { class: "stack small" },
    pool.disclosure ? h("p", null, pool.disclosure) : null,
    pool.stand_in ? h("p", { class: "stand-in" }, pool.stand_in) : null,
  );
}

export function standingsTable(state, meId, rows) {
  const list = rows || state.leaderboard || [];
  if (!list.length) return h("p", { class: "muted" }, "No one has a seat yet.");
  const max = (state.rubric && state.rubric.total_points) || 100;
  return h(
    "ol",
    { class: "standings", "aria-label": "Standings" },
    list.map((row) => {
      const pct = Math.min(100, (100 * (row.score || 0)) / max);
      const first = row.rank === 1 && (row.score || 0) > 0;
      return h(
        "li",
        { class: ["srow", row.player_id === meId ? "me" : "", first ? "first" : ""].join(" ").trim() },
        h("span", { class: "rank" }, row.rank ?? "-"),
        h(
          "span",
          { class: "who" },
          h("b", null, row.name),
          row.player_id === meId ? h("span", { class: "small muted" }, "  you") : null,
          h("span", { class: "tiny muted", style: { display: "block" } }, `${plural(row.verified_milestones || 0, "milestone")} verified`),
        ),
        h("span", { class: "score num" }, row.score ?? 0),
        h("span", { class: "sbar", "aria-hidden": "true" }, h("i", { style: { width: `${pct}%` } })),
      );
    }),
  );
}

export function milestoneList(contract, { events = [], showWindows = false } = {}) {
  const unit = (contract.target && contract.target.unit) || "";
  const verified = new Set(events.filter((e) => e.state === "VERIFIED").map((e) => e.milestone));
  const due = showWindows ? dueMilestone(contract, events) : null;
  return h(
    "div",
    { class: "milestones" },
    (contract.milestones || []).map((m) => {
      const met = verified.has(m.index);
      const isDue = due && due.index === m.index;
      return h(
        "div",
        { class: `milestone ${met ? "met" : ""} ${isDue ? "due" : ""}`.trim() },
        h("span", { class: "when" }, `Day ${m.day}`),
        h(
          "span",
          { class: "target" },
          h("b", null, `${m.target} ${unit}`),
          met ? pill("VERIFIED") : null,
        ),
        h("span", { class: "pts" }, `${m.max_points} pts`),
      );
    }),
  );
}

// The first milestone not yet verified whose window has opened; before the start, the first.
export function dueMilestone(contract, events = []) {
  const verified = new Set(events.filter((e) => e.state === "VERIFIED").map((e) => e.milestone));
  const open = (contract.milestones || []).filter((m) => !verified.has(m.index));
  if (!open.length) return null;
  const t = now();
  const opened = open.filter((m) => !m.opens_at || Date.parse(m.opens_at) <= t);
  return opened[0] || open[0];
}

export function contractCard(contract, { player, compact = false, hideStatus = false } = {}) {
  if (!contract) return h("div", { class: "seat empty" }, "No contract yet");
  const unit = (contract.target && contract.target.unit) || "";
  const baseline = contract.baseline || {};
  return h(
    "article",
    { class: `card contract ${contract.status === "PROPOSED" ? "" : "locked"}`.trim() },
    h(
      "div",
      { class: "stack" },
      h(
        "div",
        { class: "contract-head" },
        h(
          "div",
          null,
          h("div", { class: "eyebrow" }, player ? `${player.name}'s goal contract` : "Goal contract"),
          h("h3", null, contract.goal_statement),
        ),
        hideStatus ? null : pill(contract.status),
      ),
      h(
        "div",
        { class: "kv" },
        kv("Baseline", `${baseline.value} ${baseline.unit || unit}${baseline.verified ? "" : ", self-reported"}`),
        kv("Target", `${contract.target.value} ${unit}`),
        kv("Duration", `${contract.duration_days} days`),
        kv("Evidence", EVIDENCE_LABEL[contract.evidence_policy] || contract.evidence_policy),
      ),
      milestoneList(contract),
      h("div", { class: "row between small" }, h("span", { class: "muted" }, "Maximum"), h("b", null, `${contract.total_max_points} points`)),
      contract.comparability && !compact
        ? h("p", { class: "why" }, h("b", null, "Why this is comparable: "), contract.comparability, h("span", { class: "muted" }, " (the coach's view, advisory)"))
        : null,
      !compact
        ? h("p", { class: "tiny muted" }, [contract.model, contract.prompt_version, contract.rubric_version].filter(Boolean).join(" . "))
        : null,
    ),
  );
}

function kv(k, v) {
  return h("div", null, h("div", { class: "k" }, k), h("div", { class: "v" }, v));
}

export function rubricCard(rubric) {
  if (!rubric) return null;
  return h(
    "section",
    { class: "plain" },
    h(
      "div",
      { class: "stack" },
      h(
        "div",
        { class: "row between" },
        h("h2", null, "The rubric"),
        rubric.locked_at ? pill("LOCKED", `Locked ${rubric.version}`) : h("span", { class: "pill" }, rubric.version),
      ),
      h("ol", { class: "rules" }, (rubric.rules || []).map((r) => h("li", null, r))),
      h("p", { class: "small muted" }, `Milestone points: ${(rubric.milestone_points || []).join(", ")}. Total ${rubric.total_points}.`),
    ),
  );
}

const LEDGER_TYPE_LABEL = {
  "group.opened": "Group opened",
  "group.ready": "Agreement open",
  "entry.reserved": "Entry reserved",
  "entry.refunded": "Entry refunded",
  "contract.proposed": "Contract proposed",
  "contract.locked": "Contract locked",
  "contract.accepted": "Contract accepted",
  "rubric.locked": "Rubric locked",
  "group.started": "Challenge started",
  "score.recorded": "Score recorded",
  "score.disputed": "Score disputed",
  "standings.frozen": "Standings frozen",
  "group.finalized": "Group finalised",
  "quote.landed": "Quote landed",
  "policy.decided": "Gate decided",
  "purchase.claimed": "Purchase claimed",
  "checkout.created": "Checkout created",
  "checkout.completed": "Checkout completed",
  "prize.purchased": "Prize purchased",
  "group.cancelled": "Group cancelled",
};

const MONEY_TYPES = new Set([
  "entry.reserved",
  "entry.refunded",
  "quote.landed",
  "policy.decided",
  "purchase.claimed",
  "checkout.created",
  "checkout.completed",
  "prize.purchased",
]);

// Ledger rows already shown, so new ones can arrive with a flash.
const seenSeq = new Set();
let ledgerPrimed = false;

export function ledgerTail(state, { title = "The ledger" } = {}) {
  const events = state.ledger_tail || [];
  const fresh = new Set(ledgerPrimed ? events.filter((e) => !seenSeq.has(e.seq)).map((e) => e.seq) : []);
  events.forEach((e) => seenSeq.add(e.seq));
  ledgerPrimed = true;
  return h(
    "section",
    { class: "plain" },
    h(
      "div",
      { class: "stack" },
      h(
        "div",
        { class: "row between" },
        h("h2", null, title),
        state.ledger_intact === false
          ? h("span", { class: "pill rejected" }, "Chain broken")
          : h("span", { class: "pill verified" }, "Chain intact"),
      ),
      events.length
        ? h(
            "div",
            { class: "table-scroll" },
            h(
              "table",
              { class: "ledger" },
              h(
                "tbody",
                null,
                events
                  .slice()
                  .reverse()
                  .map((e) =>
                    h(
                      "tr",
                      { class: fresh.has(e.seq) ? "arrive" : null },
                      h("td", { class: "num muted" }, `#${e.seq}`),
                      h(
                        "td",
                        null,
                        h("div", null, h("span", { class: `pill ${MONEY_TYPES.has(e.type) ? "money" : ""}` }, LEDGER_TYPE_LABEL[e.type] || e.type)),
                        h("div", { style: { marginTop: "4px" } }, e.summary),
                      ),
                      h("td", { class: "r hide-phone" }, h("div", { class: "muted num" }, timeOfDay(e.at)), h("div", { class: "hash" }, (e.hash || "").slice(0, 12))),
                    ),
                  ),
              ),
            ),
          )
        : h("p", { class: "muted" }, "Nothing on the ledger yet."),
    ),
  );
}

// Last score shown per ring, so a landing score counts up from where it was.
const shownScores = new Map();

export function progressRing(score, max = 100, { key = "me", lead = false } = {}) {
  const r = 52;
  const c = 2 * Math.PI * r;
  const to = Math.max(0, score || 0);
  const from = shownScores.has(key) ? shownScores.get(key) : to;
  shownScores.set(key, to);
  const offset = (v) => c * (1 - Math.max(0, Math.min(1, v / max)));
  const ring = svg("svg", { viewBox: "0 0 120 120", "aria-hidden": "true" });
  ring.appendChild(svg("circle", { class: "track", cx: 60, cy: 60, r, fill: "none", "stroke-width": 11 }));
  const fillCircle = svg("circle", {
    class: "fill",
    cx: 60,
    cy: 60,
    r,
    fill: "none",
    "stroke-width": 11,
    "stroke-linecap": "round",
    "stroke-dasharray": c,
    "stroke-dashoffset": offset(from),
  });
  ring.appendChild(fillCircle);
  const numberEl = h("b", null, from);
  const el = h(
    "div",
    { class: `ring ${lead ? "lead" : ""}`.trim(), role: "img", "aria-label": `${to} of ${max} points` },
    ring,
    h("div", { class: "centre" }, numberEl, h("span", null, `of ${max}`)),
  );
  if (from !== to) {
    requestAnimationFrame(() => requestAnimationFrame(() => fillCircle.setAttribute("stroke-dashoffset", offset(to))));
    countUp(numberEl, from, to, 900);
  }
  return el;
}

function countUp(el, from, to, ms) {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    el.textContent = to;
    return;
  }
  const start = performance.now();
  const step = (t) => {
    const k = Math.min(1, (t - start) / ms);
    el.textContent = Math.round(from + (to - from) * (1 - Math.pow(1 - k, 3)));
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

export function playersBand(state, meId) {
  const players = (state.players || []).slice().sort((a, b) => a.seat - b.seat);
  const max = (state.rubric && state.rubric.total_points) || 100;
  const top = Math.max(0, ...players.map((p) => p.score || 0));
  return h(
    "div",
    { class: "players-band" },
    players.map((p) => {
      const lead = top > 0 && (p.score || 0) === top;
      return h(
        "div",
        { class: `player ${p.player_id === meId ? "me" : ""}`.trim() },
        progressRing(p.score || 0, max, { key: p.player_id, lead }),
        h("div", { class: "who" }, p.name),
        h("div", { class: "row", style: { justifyContent: "center" } }, lead ? h("span", { class: "pill lead" }, "Leading") : null, h("span", { class: "sub" }, `${plural(p.verified_milestones || 0, "milestone")} verified`)),
      );
    }),
  );
}

export function seatRings(state, meId) {
  const g = state.group;
  const players = state.players || [];
  return h(
    "div",
    { class: "seat-rings" },
    Array.from({ length: g.max_players }, (_, i) => {
      const p = players.find((x) => x.seat === i + 1);
      return h(
        "div",
        { class: "stack", style: { textAlign: "center" } },
        h(
          "div",
          { class: `seat-ring ${p ? "taken" : ""} ${p && p.player_id === meId ? "mine" : ""}`.trim(), "aria-label": p ? `Seat ${i + 1}: ${p.name}` : `Seat ${i + 1}: open` },
          p ? p.name.charAt(0).toUpperCase() : i + 1,
        ),
        h("div", { class: "seat-names" }, p ? p.name : "Open"),
      );
    }),
  );
}

// The agent's spending authority at a glance: the quote inside the ceiling inside the pool.
export function authorityBar(state, quoteAmount) {
  const pool = state.pool || {};
  const full = Number(state.group.entry_amount) * state.group.max_players;
  const gross = Number(pool.gross) || 0;
  const scale = Math.max(full, gross, Number(quoteAmount) || 0, 1);
  const ceiling = Number(pool.ceiling) || 0;
  const quote = Number(quoteAmount ?? pool.prize_quote ?? (state.prize && state.prize.quote && state.prize.quote.final_amount));
  const pct = (v) => `${Math.max(0, Math.min(100, (100 * v) / scale))}%`;
  const inside = Number.isFinite(quote) && quote <= ceiling;
  return h(
    "div",
    { class: "authority", role: "img", "aria-label": Number.isFinite(quote) ? `Quote ${quote.toFixed(2)} against a ceiling of ${ceiling.toFixed(2)}` : `Ceiling ${ceiling.toFixed(2)}` },
    h(
      "div",
      { class: "track" },
      h("div", { class: "ceiling", style: { width: pct(ceiling) } }),
      Number.isFinite(quote) ? h("div", { class: "quote", style: { width: `calc(${pct(quote)} - 6px)` } }) : null,
      h("div", { class: "mark", style: { left: pct(ceiling) } }),
    ),
    h(
      "div",
      { class: "legend" },
      Number.isFinite(quote) ? h("span", null, `Quote ${money(quote, "")}`) : h("span", null, "No quote yet"),
      h("span", null, `Agent may spend up to ${money(ceiling, "")}`),
      Number.isFinite(quote) ? h("span", null, inside ? "Inside the limit" : "Over the limit") : null,
    ),
  );
}

export function deadlineBlock(label, iso, doneText, state) {
  return h(
    "div",
    { class: "stack" },
    h("div", { class: "eyebrow" }, label),
    countdown(iso, { done: doneText }),
    state ? demoClockNote(state) : null,
  );
}
