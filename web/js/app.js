// The room screen: polls the group's state, keeps this browser's seat, and shows the screen
// for where the group is (or the one the viewer picked from the journey bar).

import { api } from "./api.js";
import { h, fill, groupPill, prefs, setServerNow } from "./dom.js";
import * as drop from "./screens/drop.js";
import * as join from "./screens/join.js";
import * as coach from "./screens/coach.js";
import * as agreement from "./screens/agreement.js";
import * as challenge from "./screens/challenge.js";
import * as result from "./screens/result.js";

const POLL_MS = 1500;
const PLAYER_KEY = "yrys.player";

const ROUTES = [
  { id: "drop", label: "The drop", screen: drop },
  { id: "join", label: "Join", screen: join },
  { id: "coach", label: "Coach and contract", screen: coach },
  { id: "agreement", label: "Agreement", screen: agreement },
  { id: "challenge", label: "Challenge", screen: challenge },
  { id: "result", label: "Result", screen: result },
];

const STAGE_OF_STATUS = {
  OPEN_FOR_JOINING: "join",
  INTAKE: "coach",
  READY_FOR_ACCEPTANCE: "agreement",
  ACTIVE: "challenge",
  RESULTS_PENDING: "result",
  DISPUTE_WINDOW: "result",
  FINALIZED: "result",
  FULFILLED: "result",
  CANCELLED: "drop",
  REFUNDING: "drop",
};

const els = {
  main: document.getElementById("main"),
  steps: document.getElementById("steps"),
  status: document.getElementById("group-status"),
  select: document.getElementById("player-select"),
  reset: document.getElementById("reset-btn"),
};

const app = {
  state: null,
  stateText: "",
  meId: null,
  route: null,
  mounted: null,
  lastStatus: null,
  offline: false,
};

function readPlayerFromUrl() {
  const params = new URLSearchParams(location.search);
  const fromUrl = params.get("player");
  if (fromUrl) prefs.set(PLAYER_KEY, fromUrl);
  app.meId = prefs.get(PLAYER_KEY);
}

function me() {
  const players = (app.state && app.state.players) || [];
  return players.find((p) => p.player_id === app.meId) || null;
}

function defaultRoute() {
  const status = app.state && app.state.group && app.state.group.status;
  const stage = STAGE_OF_STATUS[status] || "drop";
  if (stage === "join" && !me()) return "drop";
  if (stage === "coach" && !me()) return "agreement";
  return stage;
}

function routeFromHash() {
  const id = location.hash.replace(/^#\/?/, "");
  return ROUTES.some((r) => r.id === id) ? id : null;
}

function context() {
  return {
    state: app.state,
    me: me(),
    meId: app.meId,
    go,
    refresh: poll,
    setState,
    setMe,
  };
}

function go(id) {
  if (location.hash !== `#/${id}`) location.hash = `#/${id}`;
  else render(true);
}

function setMe(id) {
  app.meId = id;
  prefs.set(PLAYER_KEY, id);
  render(true);
}

function setState(state) {
  if (!state || !state.group) return;
  const text = JSON.stringify(state);
  const statusChanged = app.lastStatus !== null && state.group.status !== app.lastStatus;
  app.state = state;
  app.stateText = text;
  setServerNow(state.clock && state.clock.now);
  app.lastStatus = state.group.status;
  if (statusChanged) {
    go(defaultRoute());
    return;
  }
  render(false);
}

function renderChrome() {
  const status = app.state && app.state.group ? app.state.group.status : null;
  fill(els.status, status ? groupPill(status) : null);

  const players = (app.state && app.state.players) || [];
  const options = [h("option", { value: "" }, "Watching, no seat")];
  for (const p of players) options.push(h("option", { value: p.player_id }, `${p.name} (seat ${p.seat})`));
  fill(els.select, options);
  els.select.value = me() ? app.meId : "";

  const live = defaultRoute();
  const liveIndex = ROUTES.findIndex((r) => r.id === live);
  fill(
    els.steps,
    ROUTES.map((r, i) => {
      const cls = [r.id === app.route ? "here" : "", r.id === live ? "live" : "", i < liveIndex ? "done" : ""];
      return h(
        "li",
        null,
        h("a", { href: `#/${r.id}`, class: cls.join(" ").trim(), "aria-current": r.id === app.route ? "page" : null }, r.label),
      );
    }),
  );
}

function render(force) {
  if (!app.state) return;
  const route = routeFromHash() || defaultRoute();
  const key = `${route}|${me() ? app.meId : ""}`;
  renderChrome();
  if (force || !app.mounted || app.mounted.key !== key) {
    if (app.mounted && app.mounted.view.unmount) app.mounted.view.unmount();
    app.route = route;
    renderChrome();
    const screen = ROUTES.find((r) => r.id === route).screen;
    const view = screen.mount(context());
    app.mounted = { key, view };
    fill(els.main, offlineBanner(), view.el);
    window.scrollTo(0, 0);
    const here = els.steps.querySelector("a.here");
    if (here) els.steps.scrollLeft = Math.max(0, here.parentElement.offsetLeft - 16);
    return;
  }
  const banner = els.main.querySelector(".banner.offline");
  if (banner) banner.replaceWith(offlineBanner() || h("span", { class: "banner offline hidden" }));
  else if (app.offline) els.main.prepend(offlineBanner());
  if (app.mounted.view.update) app.mounted.view.update(context());
}

function offlineBanner() {
  return app.offline
    ? h("p", { class: "banner offline", role: "status" }, "The room's server cannot be reached. Retrying every few seconds.")
    : null;
}

let polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  try {
    const state = await api.state();
    const wasOffline = app.offline;
    app.offline = false;
    if (JSON.stringify(state) !== app.stateText || wasOffline || !app.mounted) setState(state);
  } catch (err) {
    if (!app.offline) {
      app.offline = true;
      render(false);
    }
    if (!app.state) fill(els.main, h("div", { class: "empty-state" }, err.message));
  } finally {
    polling = false;
  }
}

els.select.addEventListener("change", () => setMe(els.select.value || null));

els.reset.addEventListener("click", async () => {
  if (!confirm("Reset the demo group? Every seat, contract and check-in starts again.")) return;
  els.reset.disabled = true;
  try {
    const res = await api.reset();
    setMe(null);
    app.lastStatus = null;
    if (res && res.state) setState(res.state);
    go("drop");
  } catch (err) {
    alert(err.message);
  } finally {
    els.reset.disabled = false;
  }
});

window.addEventListener("hashchange", () => render(false));

readPlayerFromUrl();
poll();
setInterval(poll, POLL_MS);
