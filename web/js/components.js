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
  const image = prize.image_url
    ? h("img", { class: "prize-img", src: prize.image_url, alt: prize.name || "The prize" })
    : h("div", { class: "prize-img", role: "img", "aria-label": "No picture of the prize" }, prize.name || "The prize");
  return h(
    "div",
    { class: "prize" },
    image,
    h(
      "div",
      { class: "stack" },
      h("h2", null, prize.name || "To be announced"),
      h("p", { class: "muted" }, prize.merchant ? `From ${prize.merchant}, through Reap's catalogue` : null),
      h(
        "div",
        { class: "row" },
        prize.list_price ? h("span", null, `List price ${money(prize.list_price, "USD")}`) : null,
        quote
          ? h("span", { class: "pill money" }, `Landed quote ${money(quote.final_amount, "USD")}, ${quote.source || "quoted"}`)
          : h("span", { class: "pill pending" }, "Not yet quoted"),
      ),
      quote
        ? h(
            "p",
            { class: "small muted" },
            `Item ${money(quote.items, "")}, shipping ${money(quote.shipping, "")}, tax ${money(quote.tax, "")}, shipped to Singapore.`,
          )
        : null,
    ),
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
  if (!list.length) return h("p", { class: "muted" }, "No standings yet.");
  const max = (state.rubric && state.rubric.total_points) || 100;
  return h(
    "div",
    { class: "table-scroll" },
    h(
      "table",
      { class: "standings" },
      h("thead", null, h("tr", null, h("th", null, "#"), h("th", null, "Player"), h("th", { class: "r" }, "Points"))),
      h(
        "tbody",
        null,
        list.map((row) =>
          h(
            "tr",
            { class: row.player_id === meId ? "me" : null },
            h("td", { class: "rank" }, row.rank ?? "-"),
            h(
              "td",
              null,
              h("div", null, h("b", null, row.name), row.player_id === meId ? h("span", { class: "muted small" }, "  you") : null),
              h("div", { class: "tiny muted" }, `${plural(row.verified_milestones || 0, "milestone")} verified`,
                row.last_verified_at ? `, last at ${timeOfDay(row.last_verified_at)}` : ""),
              h("div", { class: "bar" }, h("i", { style: { width: `${Math.min(100, (100 * (row.score || 0)) / max)}%` } })),
            ),
            h("td", { class: "r score num" }, row.score ?? 0),
          ),
        ),
      ),
    ),
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

export function ledgerTail(state, { title = "The ledger" } = {}) {
  const events = state.ledger_tail || [];
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
          : h("span", { class: "pill verified" }, "Hash chain intact"),
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
                      null,
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

export function progressRing(score, max = 100) {
  const r = 52;
  const c = 2 * Math.PI * r;
  const frac = Math.max(0, Math.min(1, (score || 0) / max));
  const ring = svg("svg", { viewBox: "0 0 120 120", "aria-hidden": "true" });
  ring.appendChild(svg("circle", { class: "track", cx: 60, cy: 60, r, fill: "none", "stroke-width": 10 }));
  ring.appendChild(
    svg("circle", {
      class: "fill",
      cx: 60,
      cy: 60,
      r,
      fill: "none",
      "stroke-width": 10,
      "stroke-linecap": "round",
      "stroke-dasharray": c,
      "stroke-dashoffset": c * (1 - frac),
    }),
  );
  return h(
    "div",
    { class: "ring", role: "img", "aria-label": `${score || 0} of ${max} points` },
    ring,
    h("div", { class: "centre" }, h("b", null, score || 0), h("span", null, `of ${max} points`)),
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
