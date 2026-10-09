// Join: the entry, the disclosure and the seat. Once seated, the screen waits for the others.

import { h, fill, act, errorLine, money, pill, plural } from "../dom.js";
import { api } from "../api.js";
import { disclosure } from "../components.js";

export function mount(ctx) {
  const el = h("div", { class: "grid two" });
  const form = buildForm(ctx);
  const side = h("div", { class: "stack" });
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
  const name = h("input", { type: "text", name: "name", autocomplete: "nickname", maxlength: "32", required: true, placeholder: "Alice" });
  const agree = h("input", { type: "checkbox", name: "agree", required: true });
  const submit = h("button", { class: "btn primary block", type: "submit" }, `Reserve my seat and pay ${money(g.entry_amount)}`);

  const form = h(
    "form",
    { class: "card stack", novalidate: true },
    h("div", { class: "eyebrow" }, "Join"),
    h("h1", null, "Take a seat"),
    h("p", { class: "lead" }, "Your entry is reserved against the vault the moment you join, and written to the ledger."),
    h("label", { class: "field" }, h("span", null, "Your name, as the room will see it"), name),
    h(
      "div",
      { class: "card quiet stack" },
      h("div", { class: "row between" }, h("span", null, "Entry"), h("b", { class: "num" }, money(g.entry_amount))),
      h("div", { class: "row between" }, h("span", null, "Refund if the group does not start"), h("b", null, "In full")),
      h("div", { class: "row between" }, h("span", null, "Cash value"), h("b", null, "None, test funds")),
    ),
    h("label", { class: "check" }, agree, h("span", null, "I have read how the pool and the prize work, and that the entry is test USDC with no cash value.")),
    err,
    submit,
  );

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = name.value.trim();
    if (!value) return showLocal(err, "Enter a name for your seat.");
    if (!agree.checked) return showLocal(err, "Tick the box to confirm you have read the disclosure.");
    const res = await act(submit, err, "Reserving your seat", () => api.join(value));
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
    { class: "card stack" },
    h("div", { class: "eyebrow" }, "Seat reserved"),
    h("h1", null, `You are in, ${me.name}`),
    h("div", { class: "row" }, pill(me.entry), h("span", { class: "muted" }, `Seat ${me.seat}, ${money(g.entry_amount)}`)),
    g.status === "OPEN_FOR_JOINING"
      ? h("p", { class: "lead" }, waiting > 0 ? `Waiting for ${plural(waiting, "more player")}. The coach opens when every seat is taken.` : "Every seat is taken.")
      : h("p", { class: "lead" }, "Every seat is taken. Time to agree your goal with the coach."),
    g.status === "INTAKE" ? h("button", { class: "btn primary block", type: "button", onclick: () => ctx.go("coach") }, "Talk to the coach") : null,
  );
}

function sidePanel(ctx) {
  const { state } = ctx;
  const g = state.group;
  const players = state.players || [];
  return [
    h(
      "section",
      { class: "card stack" },
      h("h2", null, `${players.length} of ${g.max_players} seats taken`),
      h(
        "ul",
        { class: "list" },
        players.map((p) => h("li", { class: "row between" }, h("b", null, p.name), pill(p.entry))),
        players.length === 0 ? h("li", { class: "muted" }, "No one yet.") : null,
      ),
    ),
    h("section", { class: "card stack" }, h("h2", null, "Disclosure"), disclosure(state)),
  ];
}
