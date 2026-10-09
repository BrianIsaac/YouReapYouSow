// Coach chat to a goal contract: the player talks, the coach proposes, the player edits and locks.

import { h, fill, act, errorLine, showError, pill, prefs } from "../dom.js";
import { api } from "../api.js";
import { contractCard, EVIDENCE_LABEL } from "../components.js";

const OPENING =
  "Hello {name}. I am your coach for this challenge. What would you like to get better at over the next four weeks? Tell me where you are today and where you want to get to.";

export function mount(ctx) {
  if (!ctx.me) return noSeat(ctx);

  const el = h("div", { class: "grid halves" });
  const chatKey = `yrys.chat.${ctx.meId}`;
  let history = loadHistory(chatKey, ctx.me.name);
  let localContract = null;
  let contractKey = "";
  let dirty = false;

  const log = h("div", { class: "chat", "aria-live": "polite" });
  const input = h("textarea", { rows: "2", placeholder: "Type your answer", "aria-label": "Your message to the coach" });
  const send = h("button", { class: "btn primary", type: "submit" }, "Send");
  const chatErr = errorLine();
  const composer = h("form", { class: "composer" }, input, send);
  const chatCard = h(
    "section",
    { class: "card stack" },
    h("div", { class: "row between" }, h("h2", null, "Your coach"), h("span", { class: "pill accent" }, "AI, advisory")),
    log,
    chatErr,
    composer,
    h("p", { class: "tiny muted" }, "The coach proposes; you decide. Nothing is binding until you lock your contract and every player accepts."),
  );
  const right = h("div", { class: "stack" });
  const contractSlot = h("div");
  const progress = h("section", { class: "card stack" });
  right.append(contractSlot, progress);
  el.append(chatCard, right);

  function paintLog(thinking) {
    fill(
      log,
      history.map((m) => h("div", { class: `msg ${m.role}` }, m.text)),
      thinking ? h("div", { class: "msg coach thinking" }, "The coach is thinking") : null,
    );
    log.scrollTop = log.scrollHeight;
  }

  function save() {
    prefs.set(chatKey, JSON.stringify(history.slice(-40)));
  }

  composer.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || send.disabled) return;
    history.push({ role: "me", text });
    input.value = "";
    paintLog(true);
    const res = await act(send, chatErr, "Sending", () => api.intake(ctx.meId, text));
    if (res) {
      if (res.reply) history.push({ role: "coach", text: res.reply });
      if (res.contract) localContract = res.contract;
      save();
      paintContract(ctx, true);
      ctx.refresh();
    }
    paintLog(false);
    input.focus();
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      composer.requestSubmit();
    }
  });

  function currentContract(c) {
    return (c.me && c.me.contract) || localContract;
  }

  function paintContract(c, force) {
    const contract = currentContract(c);
    const key = JSON.stringify(contract);
    if (!force && key === contractKey) return;
    if (!force && dirty && contract && contract.status === "PROPOSED") return;
    contractKey = key;
    dirty = false;
    if (!contract) {
      fill(
        contractSlot,
        h(
          "section",
          { class: "card stack" },
          h("div", { class: "eyebrow" }, "Your goal contract"),
          h("p", { class: "lead" }, "Your contract appears here once the coach has heard enough: your baseline, your target, four milestones and how you will prove them."),
          h("p", { class: "small muted" }, "Everyone gets the same 100 points across four milestones."),
        ),
      );
      return;
    }
    if (contract.status === "PROPOSED" && c.state.group.status === "INTAKE") {
      fill(contractSlot, editor(c, contract, () => (dirty = true), (next) => {
        localContract = next;
        paintContract(c, true);
        c.refresh();
      }));
      return;
    }
    fill(contractSlot, contractCard(contract, { player: c.me }));
  }

  function paintProgress(c) {
    const players = c.state.players || [];
    const locked = players.filter((p) => p.intake === "LOCKED").length;
    fill(
      progress,
      h("h2", null, `${locked} of ${players.length} contracts locked`),
      h("ul", { class: "list" }, players.map((p) => h("li", { class: "row between" }, h("b", null, p.name), pill(p.intake)))),
      c.state.group.status === "READY_FOR_ACCEPTANCE"
        ? h("button", { class: "btn primary block", type: "button", onclick: () => c.go("agreement") }, "Go to the group agreement")
        : h("p", { class: "small muted" }, "When every contract is locked, the group agreement opens."),
    );
  }

  const intakeOpen = ctx.state.group.status === "INTAKE";
  composer.classList.toggle("hidden", !intakeOpen || (ctx.me.contract && ctx.me.contract.status !== "PROPOSED"));
  if (!intakeOpen && ctx.state.group.status === "OPEN_FOR_JOINING") {
    history = [{ role: "coach", text: "The coach opens once every seat is taken." }];
  }
  paintLog(false);
  paintContract(ctx, true);
  paintProgress(ctx);

  return {
    el,
    update(c) {
      const locked = c.me && c.me.contract && c.me.contract.status !== "PROPOSED";
      composer.classList.toggle("hidden", c.state.group.status !== "INTAKE" || Boolean(locked));
      paintContract(c, false);
      paintProgress(c);
    },
  };
}

function editor(ctx, contract, onDirty, onSaved) {
  const err = errorLine();
  const unit = contract.target.unit;
  const fields = {
    goal_statement: h("textarea", { rows: "2" }, contract.goal_statement),
    baseline_value: h("input", { type: "number", min: "0", step: "any", value: contract.baseline.value }),
    target_value: h("input", { type: "number", min: "0", step: "any", value: contract.target.value }),
    unit: h("input", { type: "text", value: unit }),
  };
  const msInputs = contract.milestones.map((m) => h("input", { type: "number", min: "0", step: "any", value: m.target, "aria-label": `Milestone day ${m.day} target` }));

  const save = h("button", { class: "btn ghost", type: "button" }, "Save changes");
  const lock = h("button", { class: "btn primary", type: "button" }, "Lock my contract");

  const card = h(
    "section",
    { class: "card contract stack" },
    h(
      "div",
      { class: "contract-head" },
      h("div", null, h("div", { class: "eyebrow" }, "Your goal contract, proposed"), h("h3", null, "Check it, change it, then lock it")),
      pill("PROPOSED"),
    ),
    h("label", { class: "field" }, h("span", null, "Goal"), fields.goal_statement),
    h(
      "div",
      { class: "edit-ms" },
      h("label", { class: "field" }, h("span", null, "Baseline"), fields.baseline_value),
      h("label", { class: "field" }, h("span", null, "Target"), fields.target_value),
      h("label", { class: "field" }, h("span", null, "Unit"), fields.unit),
    ),
    h("div", { class: "eyebrow" }, "Milestones (points are fixed by the rubric)"),
    h(
      "div",
      { class: "milestones" },
      contract.milestones.map((m, i) =>
        h("div", { class: "milestone" }, h("span", { class: "when" }, `Day ${m.day}`), msInputs[i], h("span", { class: "pts" }, `${m.max_points} pts`)),
      ),
    ),
    h("div", { class: "kv" }, h("div", null, h("div", { class: "k" }, "Evidence"), h("div", { class: "v" }, EVIDENCE_LABEL[contract.evidence_policy] || contract.evidence_policy)), h("div", null, h("div", { class: "k" }, "Maximum"), h("div", { class: "v" }, `${contract.total_max_points} points`))),
    contract.comparability ? h("p", { class: "why" }, h("b", null, "Why this is comparable: "), contract.comparability, h("span", { class: "muted" }, " (the coach's view, advisory)")) : null,
    err,
    h("div", { class: "row" }, save, lock),
    h("p", { class: "tiny muted" }, "Once locked, your contract cannot change. Everyone sees every contract before accepting."),
  );

  card.addEventListener("input", onDirty);

  function edits() {
    return {
      goal_statement: fields.goal_statement.value.trim(),
      baseline_value: Number(fields.baseline_value.value),
      target_value: Number(fields.target_value.value),
      unit: fields.unit.value.trim(),
      milestone_targets: msInputs.map((i) => Number(i.value)),
    };
  }

  function changed() {
    const e = edits();
    return (
      e.goal_statement !== contract.goal_statement ||
      e.baseline_value !== contract.baseline.value ||
      e.target_value !== contract.target.value ||
      e.unit !== unit ||
      e.milestone_targets.some((v, i) => v !== contract.milestones[i].target)
    );
  }

  save.addEventListener("click", async () => {
    const res = await act(save, err, "Saving", () => api.editContract(ctx.meId, edits()));
    if (res && res.contract) onSaved(res.contract);
  });

  lock.addEventListener("click", async () => {
    showError(err, null);
    const res = await act(lock, err, "Locking", async () => {
      if (changed()) await api.editContract(ctx.meId, edits());
      return api.lockContract(ctx.meId);
    });
    if (res && res.contract) onSaved(res.contract);
  });

  return card;
}

function loadHistory(key, name) {
  try {
    const saved = JSON.parse(prefs.get(key) || "null");
    if (Array.isArray(saved) && saved.length) return saved;
  } catch {
    /* a corrupt history starts afresh */
  }
  return [{ role: "coach", text: OPENING.replace("{name}", name) }];
}

function noSeat(ctx) {
  return {
    el: h(
      "section",
      { class: "card empty-state stack" },
      h("h2", null, "The coach talks to players"),
      h("p", null, "Pick your seat in the top bar, or join the drop if a seat is open."),
      h("button", { class: "btn ghost", type: "button", onclick: () => ctx.go("drop") }, "Back to the drop"),
    ),
  };
}
