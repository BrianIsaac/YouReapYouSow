// Home: the drops on offer, as a poster grid. Each tile is one drop and opens its page.

import { h, fill, countdown, money, groupPill, now, prefs } from "../dom.js";

const TONES = ["wheat", "field", "sky", "paper", "night"];

export function mount(ctx) {
  const el = h("div", { class: "stack-lg" });
  const paint = (c) => fill(el, build(c));
  paint(ctx);
  return { el, update: paint };
}

function build(ctx) {
  const list = (ctx.drops || []).map(asDrop).filter(Boolean);
  const order = list.slice().sort((a, b) => rankOf(a) - rankOf(b));
  return [
    h(
      "section",
      { class: "stack" },
      h("h1", { class: "drops-title" }, "Pick a drop"),
      h("p", { class: "lead" }, "Each drop is one prize and three seats. Agree a goal with the coach, prove your progress, and the most verified points wins the prize, bought by the agent from the pool."),
    ),
    order.length
      ? h("section", { class: "drops-grid", "aria-label": "Drops" }, order.map((d, i) => tile(d, i)))
      : h("p", { class: "empty-state" }, "No drops are open right now."),
  ];
}

// Open drops first, soonest deadline first; then live ones; finished ones last.
function rankOf(d) {
  const s = d.group.status;
  const base = s === "OPEN_FOR_JOINING" ? 0 : ["INTAKE", "READY_FOR_ACCEPTANCE", "ACTIVE"].includes(s) ? 1 : 2;
  const t = Date.parse(d.group.enrolment_deadline || d.group.ends_at || "") || 0;
  return base * 1e13 + t;
}

// Accepts a drop as a full state ({group, prize, pool, players}) or as a flat summary.
function asDrop(d) {
  if (!d) return null;
  if (d.group) return d;
  const seats = Array.isArray(d.players) ? d.players : Array.from({ length: d.seats_taken || 0 }, (_, i) => ({ seat: i + 1, name: "" }));
  return {
    group: {
      id: d.id || d.drop_id,
      title: d.title,
      status: d.status,
      entry_amount: d.entry_amount,
      min_players: d.min_players || 3,
      max_players: d.max_players || 3,
      duration_days: d.duration_days,
      enrolment_deadline: d.enrolment_deadline,
      scheduled_start: d.starts_at || d.scheduled_start,
      scheduled_end: d.ends_at || d.scheduled_end,
      started_at: d.started_at,
      ends_at: d.ends_at,
    },
    clock: d.clock,
    prize: d.prize || {},
    pool: d.pool || {},
    players: seats,
  };
}

function tile(d, i) {
  const g = d.group;
  const tone = TONES[i % TONES.length];
  const players = d.players || [];
  const open = g.status === "OPEN_FOR_JOINING";
  const live = g.status === "ACTIVE";
  const mine = prefs.get(`yrys.player.${g.id}`);
  const seated = mine && players.some((p) => p.player_id === mine);
  const full = (Number(g.entry_amount) || 0) * g.max_players;
  const initial = ((d.prize && (d.prize.merchant || d.prize.name)) || "?").trim().charAt(0).toUpperCase();
  return h(
    "a",
    { class: `drop-tile block ${tone} ${i === 0 ? "featured" : ""}`.trim(), href: `#/d/${encodeURIComponent(g.id)}`, "aria-label": `${g.title}: ${statusWords(g.status)}` },
    h(
      "div",
      { class: "tile-head" },
      d.prize && d.prize.image_url ? h("img", { class: "tile-img", src: d.prize.image_url, alt: "" }) : h("div", { class: "tile-mark", "aria-hidden": "true" }, initial),
      h("div", { class: "row", style: { justifyContent: "flex-end" } }, seated ? h("span", { class: "pill accent" }, "Your seat") : null, groupPill(g.status)),
    ),
    h("h2", null, g.title),
    h("p", { class: "small muted" }, d.prize && d.prize.name ? d.prize.name : ""),
    h(
      "div",
      { class: "tile-figures" },
      h("div", null, h("div", { class: "label" }, "Entry"), h("div", { class: "tile-num" }, money(g.entry_amount, ""))),
      h("div", null, h("div", { class: "label" }, "Pool"), h("div", { class: "tile-num" }, money(d.pool.gross || 0, "")), h("div", { class: "tiny muted" }, `of ${money(full, "")}`)),
      h("div", null, h("div", { class: "label" }, "Seats"), h("div", { class: "tile-num" }, `${players.length}/${g.max_players}`)),
    ),
    h("div", { class: "tile-dots", "aria-hidden": "true" }, Array.from({ length: g.max_players }, (_, k) => h("i", { class: k < players.length ? "on" : "" }))),
    h(
      "div",
      { class: "tile-foot" },
      h("div", null, h("div", { class: "label" }, `${g.duration_days} days in demo time`), h("div", { class: "small" }, dateRange(d))),
      open || live
        ? h("div", { class: "tile-clock" }, h("div", { class: "label" }, open ? "Enrolment closes in" : "Ends in"), countdown(open ? g.enrolment_deadline : g.ends_at, { done: "Closed", cls: "countdown tile-count" }))
        : null,
    ),
  );
}

function statusWords(status) {
  return {
    OPEN_FOR_JOINING: "open for joining",
    INTAKE: "setting goals",
    READY_FOR_ACCEPTANCE: "agreement",
    ACTIVE: "challenge live",
    RESULTS_PENDING: "results pending",
    DISPUTE_WINDOW: "dispute window",
    FINALIZED: "finalised",
    FULFILLED: "prize bought",
    CANCELLED: "cancelled",
  }[status] || "";
}

function dateRange(d) {
  const g = d.group;
  const perDay = d.clock && d.clock.seconds_per_day;
  const start = Date.parse(g.started_at || g.scheduled_start || g.enrolment_deadline || "");
  let end = Date.parse(g.ends_at || g.scheduled_end || "");
  if (Number.isNaN(end) && !Number.isNaN(start) && perDay) end = start + perDay * g.duration_days * 1000;
  if (Number.isNaN(start)) return "";
  const fmt = (t) => new Date(t).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  const day = (t) => {
    const date = new Date(t);
    const today = new Date(now());
    return date.toDateString() === today.toDateString() ? "today" : date.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  };
  return Number.isNaN(end) ? `From ${fmt(start)} ${day(start)}` : `${fmt(start)} to ${fmt(end)} ${day(end)}`;
}
