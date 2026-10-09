// Small DOM and formatting helpers shared by every screen.

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
      else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
      else if (key === "html") el.innerHTML = value;
      else if (value === true) el.setAttribute(key, "");
      else el.setAttribute(key, value);
    }
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) append(el, child);
    else if (child instanceof Node) el.appendChild(child);
    else el.appendChild(document.createTextNode(String(child)));
  }
}

export function fill(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

export function svg(tag, attrs) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs || {})) el.setAttribute(key, value);
  return el;
}

// Server time: the offset between the server's clock and this browser's, set on each poll.
let clockOffsetMs = 0;
export function setServerNow(iso) {
  const t = Date.parse(iso || "");
  if (!Number.isNaN(t)) clockOffsetMs = t - Date.now();
}
export function now() {
  return Date.now() + clockOffsetMs;
}

export function money(amount, unit = "test USDC") {
  if (amount === null || amount === undefined || amount === "") return "-";
  const n = Number(amount);
  const text = Number.isFinite(n) ? n.toFixed(2) : String(amount);
  return unit ? `${text} ${unit}` : text;
}

export function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many || one + "s"}`;
}

export function clockText(ms) {
  if (ms <= 0) return "00:00";
  const total = Math.ceil(ms / 1000);
  const hours = Math.floor(total / 3600);
  const mins = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const mm = String(mins).padStart(2, "0");
  const ss = String(secs).padStart(2, "0");
  return hours ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function timeOfDay(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function msUntil(iso) {
  const t = Date.parse(iso || "");
  return Number.isNaN(t) ? null : t - now();
}

// A live countdown: any element made here is refreshed by the ticker below.
export function countdown(iso, { done = "Time is up", cls = "countdown" } = {}) {
  const el = h("span", { class: cls, "data-deadline": iso || "", "data-done": done, role: "timer" });
  paintCountdown(el);
  return el;
}

function paintCountdown(el) {
  const left = msUntil(el.dataset.deadline);
  if (left === null) el.textContent = "-";
  else if (left <= 0) el.textContent = el.dataset.done;
  else el.textContent = clockText(left);
}

setInterval(() => {
  document.querySelectorAll("[data-deadline]").forEach(paintCountdown);
}, 250);

const PILLS = {
  PENDING: ["pending", "Pending"],
  VERIFIED: ["verified", "Verified"],
  REJECTED: ["rejected", "Rejected"],
  DISPUTED: ["disputed", "Disputed"],
  RESERVED: ["money", "Entry reserved"],
  REFUNDED: ["money", "Refunded"],
  NOT_STARTED: ["", "Not started"],
  CHATTING: ["pending", "With the coach"],
  PROPOSED: ["pending", "Proposed"],
  LOCKED: ["verified", "Locked"],
  ACCEPTED: ["verified", "Accepted"],
  BUYING: ["money", "Buying"],
  PURCHASED: ["verified", "Purchased"],
  FAILED: ["rejected", "Failed"],
};

export function pill(state, label) {
  const [cls, text] = PILLS[state] || ["", state ? String(state).replaceAll("_", " ").toLowerCase() : "-"];
  return h("span", { class: `pill ${cls}`.trim() }, label || text);
}

export const GROUP_STATES = [
  "OPEN_FOR_JOINING",
  "INTAKE",
  "READY_FOR_ACCEPTANCE",
  "ACTIVE",
  "RESULTS_PENDING",
  "DISPUTE_WINDOW",
  "FINALIZED",
  "FULFILLED",
];

export const GROUP_STATE_LABEL = {
  OPEN_FOR_JOINING: "Open for joining",
  INTAKE: "Setting goals",
  READY_FOR_ACCEPTANCE: "Agreement",
  ACTIVE: "Challenge live",
  RESULTS_PENDING: "Results pending",
  DISPUTE_WINDOW: "Dispute window",
  FINALIZED: "Finalised",
  FULFILLED: "Prize bought",
  CANCELLED: "Cancelled",
  REFUNDING: "Refunding",
};

export function groupPill(status) {
  const cls = {
    OPEN_FOR_JOINING: "accent",
    INTAKE: "pending",
    READY_FOR_ACCEPTANCE: "pending",
    ACTIVE: "verified",
    RESULTS_PENDING: "disputed",
    DISPUTE_WINDOW: "disputed",
    FINALIZED: "money",
    FULFILLED: "verified",
    CANCELLED: "rejected",
    REFUNDING: "money",
  }[status] || "";
  return h("span", { class: `pill ${cls}` }, GROUP_STATE_LABEL[status] || status || "-");
}

export function stateStrip(status) {
  const at = GROUP_STATES.indexOf(status);
  return h(
    "ol",
    { class: "state-strip", "aria-label": "Group state" },
    GROUP_STATES.map((s, i) =>
      h("li", { class: i === at ? "now" : i < at ? "past" : "" }, GROUP_STATE_LABEL[s]),
    ),
  );
}

export function errorLine() {
  return h("p", { class: "banner error hidden", role: "alert" });
}

export function showError(el, err) {
  if (!el) return;
  if (!err) {
    el.classList.add("hidden");
    el.textContent = "";
    return;
  }
  el.textContent = err.message || String(err);
  el.classList.remove("hidden");
}

// Runs an async action from a button: disables it, shows the label while busy, reports errors.
export async function act(button, errorEl, busyLabel, fn) {
  const label = button.textContent;
  button.disabled = true;
  if (busyLabel) button.textContent = busyLabel;
  showError(errorEl, null);
  try {
    return await fn();
  } catch (err) {
    showError(errorEl, err);
    return undefined;
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

export const prefs = {
  get(key) {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  set(key, value) {
    try {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch {
      /* storage unavailable: the seat is held for this page only */
    }
  },
};
