// The room screen: the drops on offer, and inside one drop the screen for where its group is
// (or the one the viewer picked from the journey bar). Each drop keeps its own seat.

import { api, useDrop } from "./api.js";
import { h, fill, groupPill, prefs, setServerNow } from "./dom.js";
import * as drops from "./screens/drops.js";
import * as drop from "./screens/drop.js";
import * as join from "./screens/join.js";
import * as coach from "./screens/coach.js";
import * as agreement from "./screens/agreement.js";
import * as challenge from "./screens/challenge.js";
import * as result from "./screens/result.js";

const POLL_MS = 1500;
const DROPS_POLL_MS = 3000;
const LEGACY = "main";

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
  whoami: document.querySelector(".whoami"),
  reset: document.getElementById("reset-btn"),
};

const app = {
  legacy: false,
  drops: null,
  dropsText: "",
  dropId: null,
  state: null,
  stateText: "",
  meId: null,
  route: null,
  mounted: null,
  lastStatus: null,
  offline: false,
};

function playerKey(id) {
  return `yrys.player.${id}`;
}

function parseHash() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (parts[0] === "d" && parts[1]) {
    const screen = ROUTES.some((r) => r.id === parts[2]) ? parts[2] : null;
    return { dropId: decodeURIComponent(parts[1]), screen };
  }
  if (ROUTES.some((r) => r.id === parts[0])) return { dropId: app.legacy ? LEGACY : null, screen: parts[0] };
  return { dropId: null, screen: null };
}

function me() {
  const players = (app.state && app.state.players) || [];
  return players.find((p) => p.player_id === app.meId) || null;
}

function defaultScreen() {
  const status = app.state && app.state.group && app.state.group.status;
  const stage = STAGE_OF_STATUS[status] || "drop";
  if (stage === "join" && !me()) return "drop";
  if (stage === "coach" && !me()) return "agreement";
  return stage;
}

function dropHref(id, screen) {
  return `#/d/${encodeURIComponent(id)}${screen ? `/${screen}` : ""}`;
}

function context() {
  return {
    state: app.state,
    drops: app.drops,
    me: me(),
    meId: app.meId,
    dropId: app.dropId,
    go,
    openDrop: (id) => (location.hash = dropHref(id)),
    refresh: poll,
    setState,
    setMe,
  };
}

function go(screen) {
  const target = dropHref(app.dropId, screen);
  if (location.hash !== target) location.hash = target;
  else render(true);
}

function setMe(id) {
  app.meId = id;
  if (app.dropId) prefs.set(playerKey(app.dropId), id);
  render(true);
}

function enterDrop(id) {
  if (app.dropId === id) return;
  app.dropId = id;
  useDrop(id === LEGACY ? null : id);
  app.state = null;
  app.stateText = "";
  app.lastStatus = null;
  app.meId = prefs.get(playerKey(id));
  app.mounted = null;
  const fromUrl = new URLSearchParams(location.search).get("player");
  if (fromUrl) {
    app.meId = fromUrl;
    prefs.set(playerKey(id), fromUrl);
  }
}

function leaveDrop() {
  app.dropId = null;
  app.state = null;
  app.stateText = "";
  app.mounted = null;
}

function setState(state) {
  if (!state || !state.group) return;
  const statusChanged = app.lastStatus !== null && state.group.status !== app.lastStatus;
  app.state = state;
  app.stateText = JSON.stringify(state);
  setServerNow(state.clock && state.clock.now);
  app.lastStatus = state.group.status;
  if (statusChanged) {
    go(defaultScreen());
    return;
  }
  render(false);
}

function renderChrome() {
  const inDrop = Boolean(app.dropId && app.state);
  const status = inDrop ? app.state.group.status : null;
  fill(els.status, status ? groupPill(status) : null);
  els.whoami.classList.toggle("hidden", !inDrop);
  els.reset.classList.toggle("hidden", !inDrop);

  if (inDrop) {
    const players = app.state.players || [];
    const options = [h("option", { value: "" }, "Watching, no seat")];
    for (const p of players) options.push(h("option", { value: p.player_id }, `${p.name} (seat ${p.seat})`));
    fill(els.select, options);
    els.select.value = me() ? app.meId : "";
  }

  const live = inDrop ? defaultScreen() : null;
  const liveIndex = ROUTES.findIndex((r) => r.id === live);
  const items = [
    app.legacy
      ? null
      : h("li", null, h("a", { href: "#/drops", class: app.route === "drops" ? "here" : "", "aria-current": app.route === "drops" ? "page" : null }, "All drops")),
  ];
  if (inDrop) {
    ROUTES.forEach((r, i) => {
      const cls = [r.id === app.route ? "here" : "", r.id === live ? "live" : "", i < liveIndex ? "done" : ""];
      items.push(
        h("li", null, h("a", { href: dropHref(app.dropId, r.id), class: cls.join(" ").trim(), "aria-current": r.id === app.route ? "page" : null }, r.label)),
      );
    });
  }
  fill(els.steps, items);
}

function render(force) {
  const { dropId, screen } = parseHash();
  if (!dropId) {
    if (!app.drops) return;
    const key = "drops";
    renderChrome();
    if (force || !app.mounted || app.mounted.key !== key) {
      app.route = "drops";
      renderChrome();
      const view = drops.mount(context());
      app.mounted = { key, view };
      fill(els.main, offlineBanner(), view.el);
      window.scrollTo(0, 0);
    } else if (app.mounted.view.update) app.mounted.view.update(context());
    return;
  }
  if (!app.state) return;
  const route = screen || defaultScreen();
  const key = `${dropId}|${route}|${me() ? app.meId : ""}`;
  renderChrome();
  if (force || !app.mounted || app.mounted.key !== key) {
    if (app.mounted && app.mounted.view.unmount) app.mounted.view.unmount();
    app.route = route;
    renderChrome();
    const view = ROUTES.find((r) => r.id === route).screen.mount(context());
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

async function loadDrops() {
  try {
    const res = await api.drops();
    app.legacy = false;
    return (res && res.drops) || [];
  } catch (err) {
    if (err.status !== 404) throw err;
    // An older server with one drop: show it as the only one.
    app.legacy = true;
    useDrop(null);
    const state = await api.state();
    if (app.dropId && app.dropId !== LEGACY) useDrop(app.dropId);
    return [{ ...state, group: { ...state.group, id: LEGACY } }];
  }
}

let polling = false;
let lastDropsPoll = 0;
async function poll() {
  if (polling) return;
  polling = true;
  try {
    const { dropId } = parseHash();
    const wasOffline = app.offline;
    if (!dropId) {
      if (app.dropId) leaveDrop();
      if (Date.now() - lastDropsPoll >= DROPS_POLL_MS || !app.drops || wasOffline) {
        lastDropsPoll = Date.now();
        const list = await loadDrops();
        app.offline = false;
        if (app.legacy && list.length === 1) {
          location.replace(dropHref(LEGACY));
          return;
        }
        const text = JSON.stringify(list);
        if (text !== app.dropsText || wasOffline || !app.mounted) {
          app.drops = list;
          app.dropsText = text;
          if (list[0] && list[0].clock) setServerNow(list[0].clock.now);
          render(false);
        }
      }
      return;
    }
    if (app.dropId !== dropId) {
      if (dropId === LEGACY) app.legacy = true;
      enterDrop(dropId);
    }
    const state = await api.state();
    app.offline = false;
    if (JSON.stringify(state) !== app.stateText || wasOffline || !app.mounted) setState(state);
  } catch (err) {
    if (!app.offline) {
      app.offline = true;
      render(false);
    }
    if (!app.state && !app.drops) fill(els.main, h("div", { class: "empty-state" }, err.message));
  } finally {
    polling = false;
  }
}

els.select.addEventListener("change", () => setMe(els.select.value || null));

els.reset.addEventListener("click", async () => {
  if (!confirm("Reset this drop? Every seat, contract and check-in in it starts again.")) return;
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

window.addEventListener("hashchange", () => {
  const { dropId } = parseHash();
  if (dropId !== app.dropId) {
    app.mounted = null;
    poll();
  } else render(false);
});

poll();
setInterval(poll, POLL_MS);
