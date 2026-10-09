// Join: the entry and the disclosure on a sheet to sign, beside the pool your entry adds to.

import { h, fill, act, errorLine, money, pill, plural } from "../dom.js";
import { api } from "../api.js";
import { disclosure, seatRings, authorityBar } from "../components.js";

export function mount(ctx) {
  const el = h("div", { class: "grid two" });
  const form = buildForm(ctx);
  const side = h("div", { class: "stack-lg" });
  const main = h("div", { class: "stack" });
  el.append(main, side);

  const paint = (c) => {
    if (c.me) fill(main, seated(c));
    else if (!main.contains(form)) fill(main, form);
    fill(side, sidePanel(c));
    form.querySelector("button[type=submit]").disabled = !canJoin(c);
  };
  paint(ctx);
  return { el, update: paint };
}

function canJoin(ctx) {
  const g = ctx.state.group;
  return g.status === "OPEN_FOR_JOINING" && (ctx.state.players || []).length < g.max_players;
}

function buildForm(ctx) {
  const g = ctx.state.group;
  const err = errorLine();
  const name = h("input", { type: "text", name: "name", autocomplete: "nickname", maxlength: "32", required: true, placeholder: "Your first name" });
  const agree = h("input", { type: "checkbox", name: "agree", required: true });
  const submit = h("button", { class: "btn primary block", type: "submit" }, `Join for ${money(g.entry_amount)}`);

  const form = h(
    "form",
    { class: "block white stack", novalidate: true },
    h("h1", null, "Take a seat"),
    h("p", { class: "lead muted" }, "Your entry is held against the vault the moment you join, and the hold is written to the ledger."),
    h("label", { class: "field" }, h("span", null, "Your name, as the room will see it"), name),
    h(
      "div",
      { class: "receipt", style: { background: "var(--paper-2)" } },
      h("div", { class: "line" }, h("span", { class: "muted" }, "Your entry"), h("b", { class: "num" }, money(g.entry_amount))),
      h("div", { class: "line" }, h("span", { class: "muted" }, "If the group does not start"), h("b", null, "Refunded in full")),
      h("div", { class: "line" }, h("span", { class: "muted" }, "Cash value"), h("b", null, "None, test funds")),
    ),
    h("label", { class: "check" }, agree, h("span", null, "I have read how the pool and the prize work, and I know the entry is test USDC with no cash value.")),
    err,
    submit,
  );

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = name.value.trim();
    if (!value) return showLocal(err, "Enter a name for your seat.");
    if (!agree.checked) return showLocal(err, "Tick the box to confirm you have read how the money works.");
    const res = await act(submit, err, "Holding your seat", () => api.join(value));
    if (res && res.player_id) {
      ctx.setMe(res.player_id);
      if (res.state) ctx.setState(res.state);
    }
  });
  return form;
}

function showLocal(el, message) {
  el.textContent = message;
  el.classList.remove("hidden");
}

function seated(ctx) {
  const { state, me } = ctx;
  const g = state.group;
  const waiting = g.max_players - (state.players || []).length;
  return h(
    "section",
    { class: "block bright stack" },
    h("div", { class: "row" }, pill(me.entry), h("span", { class: "label" }, `Seat ${me.seat}, ${money(g.entry_amount)}`)),
    h("h1", null, `You are in, ${me.name}`),
    g.status === "OPEN_FOR_JOINING"
      ? h("p", { class: "lead", style: { color: "var(--ink)" } }, waiting > 0 ? `Waiting for ${plural(waiting, "more player")}. The coach opens when every seat is taken.` : "Every seat is taken.")
      : h("p", { class: "lead", style: { color: "var(--ink)" } }, "Every seat is taken. Set your goal with the coach."),
    g.status === "INTAKE" ? h("button", { class: "btn block", type: "button", onclick: () => ctx.go("coach") }, "Talk to the coach") : null,
  );
}

function sidePanel(ctx) {
  const { state, me } = ctx;
  const g = state.group;
  const pool = state.pool || {};
  const after = (Number(pool.gross) || 0) + (me ? 0 : Number(g.entry_amount));
  return [
    h(
      "section",
      { class: "block strong stack" },
      h("div", { class: "label" }, me ? "The pool" : "The pool once you join"),
      h("div", { class: "numeral", style: { fontSize: "clamp(3.6rem, 10vw, 6rem)" } }, money(after, "")),
      h("p", { class: "small" }, me ? "test USDC held against the vault." : `test USDC: ${money(pool.gross, "")} now, plus your ${money(g.entry_amount, "")}.`),
      authorityBar(state),
    ),
    h("section", { class: "block white stack" }, h("div", { class: "label" }, `${(state.players || []).length} of ${g.max_players} seats taken`), seatRings(state, ctx.meId)),
    h("section", { class: "plain stack" }, h("h2", null, "Before you pay"), disclosure(state)),
  ];
}
