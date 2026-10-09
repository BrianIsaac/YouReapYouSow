// Active challenge: my progress ring, the due milestone, the check-in with camera or upload,
// the countdown and the standings.

import { h, fill, act, errorLine, showError, pill, countdown, timeOfDay } from "../dom.js";
import { api } from "../api.js";
import { progressRing, milestoneList, dueMilestone, standingsTable, demoClockNote, ledgerTail, EVIDENCE_LABEL } from "../components.js";

const MAX_BYTES = 8 * 1024 * 1024;

export function mount(ctx) {
  const el = h("div", { class: "stack-lg" });
  const header = h("section", { class: "grid two" });
  const body = h("div", { class: "grid two" });
  const left = h("div", { class: "stack-lg" });
  const right = h("div", { class: "stack-lg" });
  body.append(left, right);
  el.append(header, body);

  let events = [];
  let latest = null;
  let pendingRow = null;
  let ctxNow = ctx;
  const camera = ctx.me ? createCamera() : null;
  const checkin = ctx.me
    ? createCheckin(
        () => ctxNow,
        (event) => {
          latest = event;
          loadEvents();
        },
        camera,
        (row) => {
          pendingRow = row;
          paint(ctxNow);
        },
      )
    : null;

  const progressSlot = h("section", { class: "card" });
  const eventsSlot = h("section", { class: "card stack" });
  const standingsSlot = h("section", { class: "card stack" });
  const feedSlot = h("div");

  if (ctx.me) left.append(progressSlot, checkin.el, eventsSlot);
  else left.append(standingsSlot);
  right.append(ctx.me ? standingsSlot : h("div"), feedSlot);

  async function loadEvents() {
    try {
      const res = await api.events(ctxNow.me ? ctxNow.meId : null);
      events = (res && res.events) || [];
    } catch {
      /* the next poll retries */
    }
    paint(ctxNow);
  }

  function paint(c) {
    ctxNow = c;
    const g = c.state.group;
    fill(header, headerBlock(c));
    if (c.me) {
      fill(progressSlot, progressBlock(c, events));
      checkin.update(c, events);
      fill(eventsSlot, eventsBlock(events, latest, pendingRow));
    }
    fill(standingsSlot, h("div", { class: "row between" }, h("h2", null, "Standings"), g.status === "ACTIVE" ? pill("PENDING", "Live") : pill("LOCKED", "Frozen")), standingsTable(c.state, c.meId));
    fill(feedSlot, ledgerTail(c.state, { title: "What just happened" }));
  }

  paint(ctx);
  loadEvents();

  return {
    el,
    update(c) {
      paint(c);
      loadEvents();
    },
    unmount() {
      if (camera) camera.stop();
    },
  };
}

function headerBlock(ctx) {
  const { state } = ctx;
  const g = state.group;
  const day = state.clock && typeof state.clock.day === "number" ? Math.min(g.duration_days, Math.max(0, state.clock.day)) : null;
  return [
    h(
      "div",
      { class: "stack" },
      h("div", { class: "eyebrow" }, g.status === "ACTIVE" ? "The challenge is live" : "The challenge"),
      h("h1", null, g.title),
      h("div", { class: "row" }, day !== null ? h("span", { class: "lead" }, `Day ${Math.floor(day)} of ${g.duration_days}`) : null, demoClockNote(state)),
    ),
    h(
      "div",
      { class: "card stack" },
      h("div", { class: "eyebrow" }, "Submissions close in"),
      countdown(g.ends_at, { done: "Closed" }),
      h("p", { class: "small muted" }, `Ends at ${timeOfDay(g.ends_at)}, real time.`),
    ),
  ];
}

function progressBlock(ctx, events) {
  const { me } = ctx;
  const contract = me.contract;
  if (!contract) return h("p", { class: "muted" }, "No contract on this seat.");
  const due = dueMilestone(contract, events);
  const unit = contract.target.unit;
  return h(
    "div",
    { class: "stack" },
    h(
      "div",
      { class: "row", style: { gap: "28px", alignItems: "center" } },
      progressRing(me.score || 0, contract.total_max_points || 100),
      h(
        "div",
        { class: "stack", style: { flex: "1", minWidth: "200px" } },
        h("div", { class: "eyebrow" }, "Your goal"),
        h("h2", null, contract.goal_statement),
        h("p", { class: "muted" }, `From ${contract.baseline.value} to ${contract.target.value} ${unit}`),
        due
          ? h(
              "div",
              { class: "milestone due" },
              h("span", { class: "when" }, "Due now"),
              h("span", null, h("b", null, `${due.target} ${unit}`), h("span", { class: "muted small" }, `  day ${due.day} milestone`)),
              h("span", { class: "pts" }, `${due.max_points} pts`),
            )
          : h("p", null, pill("VERIFIED", "Every milestone verified")),
      ),
    ),
    milestoneList(contract, { events, showWindows: true }),
  );
}

function eventsBlock(events, latest, pendingRow) {
  const list = events.slice().sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
  return [
    h("h2", null, "Your check-ins"),
    list.length || pendingRow
      ? h(
          "ul",
          { class: "list checkins" },
          pendingRow
            ? h(
                "li",
                { class: "ci", style: { background: "var(--pending-bg)", borderRadius: "10px", padding: "14px" } },
                h("div", null, h("b", null, `Milestone ${pendingRow.milestone + 1}`), h("span", { class: "muted small" }, `  claimed ${pendingRow.value}, ${pendingRow.kind}`)),
                h("div", { class: "row" }, pill("PENDING")),
                h("div", { class: "why-line" }, pendingRow.kind === "log" ? "Recording your log against the rubric." : "Reading your evidence and scoring it by the rubric."),
              )
            : null,
          list.map((e) => eventRow(e, latest && latest.event_id === e.event_id)),
        )
      : h("p", { class: "muted" }, "No check-ins yet. Your first one is waiting."),
  ];
}

export function eventRow(e, highlight) {
  const adv = e.advisory;
  return h(
    "li",
    { class: "ci", style: highlight ? { background: "var(--bg)", borderRadius: "10px", padding: "14px" } : null },
    h(
      "div",
      null,
      h("b", null, `Milestone ${e.milestone + 1}`),
      h("span", { class: "muted small" }, `  claimed ${e.claimed_value}, ${e.evidence_kind}, ${timeOfDay(e.at)}`),
    ),
    h("div", { class: "row" }, pill(e.state), h("b", { class: "num" }, e.delta > 0 ? `+${e.delta}` : "0")),
    e.reason ? h("div", { class: "why-line" }, e.reason) : null,
    adv && adv.note
      ? h(
          "div",
          { class: "why-line" },
          `Vision model${adv.model ? ` (${adv.model.split("/").pop()})` : ""}, advisory: `,
          adv.shows === true ? "shows the exercise" : adv.shows === false ? "does not show the exercise" : "",
          typeof adv.count === "number" ? `, counts ${adv.count}` : "",
          adv.note ? `. ${adv.note}` : "",
        )
      : null,
    e.evidence_id ? h("div", { class: "why-line" }, h("a", { href: api.evidenceUrl(e.evidence_id), target: "_blank", rel: "noopener" }, "View the evidence")) : null,
  );
}

function createCheckin(getCtx, onRecorded, camera, onPending) {
  const err = errorLine();
  const select = h("select", { class: "input", "aria-label": "Milestone" });
  const value = h("input", { type: "number", min: "0", step: "any", inputmode: "decimal" });
  const note = h("input", { type: "text", maxlength: "200", placeholder: "Optional, for the log" });
  const file = h("input", { type: "file", accept: "image/*,video/*" });
  const preview = h("div", { class: "camera" }, h("div", { class: "placeholder" }, "No evidence yet. Start the camera or choose a photo or clip."));
  const startCam = h("button", { class: "btn ghost", type: "button" }, "Start the camera");
  const snap = h("button", { class: "btn", type: "button", disabled: true }, "Take the photo");
  const clear = h("button", { class: "btn ghost hidden", type: "button" }, "Remove");
  const submit = h("button", { class: "btn primary block", type: "submit" }, "Submit the check-in");
  const policyLine = h("p", { class: "small muted" });
  const closed = h("p", { class: "banner hidden" }, "Submissions are closed. The standings are frozen.");
  let evidence = null;
  let lastOptions = "";
  let valueTouched = false;
  let contractNow = null;
  value.addEventListener("input", () => (valueTouched = true));
  select.addEventListener("change", () => {
    const m = contractNow && contractNow.milestones.find((x) => String(x.index) === select.value);
    if (m) value.value = m.target;
    valueTouched = false;
  });

  function setEvidence(blob, kind) {
    evidence = blob;
    camera.stop();
    snap.disabled = true;
    clear.classList.toggle("hidden", !blob);
    if (!blob) {
      fill(preview, h("div", { class: "placeholder" }, "No evidence yet. Start the camera or choose a photo or clip."));
      return;
    }
    const url = URL.createObjectURL(blob);
    fill(preview, kind === "video" ? h("video", { src: url, controls: true, muted: true, playsinline: true }) : h("img", { src: url, alt: "Your evidence" }));
  }

  startCam.addEventListener("click", async () => {
    showError(err, null);
    try {
      const video = await camera.start();
      evidence = null;
      fill(preview, video);
      snap.disabled = false;
      clear.classList.add("hidden");
    } catch (e) {
      showError(err, new Error(`The camera is not available here (${e.name || "error"}). Choose a photo or clip instead.`));
    }
  });
  snap.addEventListener("click", async () => {
    const blob = await camera.capture();
    if (blob) setEvidence(new File([blob], "checkin.jpg", { type: "image/jpeg" }), "image");
  });
  clear.addEventListener("click", () => {
    file.value = "";
    setEvidence(null);
  });
  file.addEventListener("change", () => {
    const f = file.files && file.files[0];
    if (!f) return;
    if (f.size > MAX_BYTES) {
      showError(err, new Error("That file is over 8 MB. Choose a shorter clip or a photo."));
      file.value = "";
      return;
    }
    showError(err, null);
    setEvidence(f, f.type.startsWith("video/") ? "video" : "image");
  });

  const form = h(
    "form",
    { class: "card stack", novalidate: true },
    h("div", { class: "row between" }, h("h2", null, "Check in"), h("span", { class: "small muted" }, "Scored by the rubric")),
    closed,
    h("div", { class: "pair" }, h("label", { class: "field" }, h("span", null, "Milestone"), select), h("label", { class: "field" }, h("span", null, "What you did"), value)),
    preview,
    h(
      "div",
      { class: "row" },
      startCam,
      snap,
      h("label", { class: "btn ghost file-label" }, "Choose a photo or clip", file),
      clear,
    ),
    policyLine,
    h("label", { class: "field" }, h("span", null, "Note"), note),
    err,
    submit,
  );

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const c = getCtx();
    const contract = c.me.contract;
    if (value.value === "") return showError(err, new Error("Enter what you did, for example the reps you managed."));
    if (contract.evidence_policy !== "log" && !evidence) return showError(err, new Error("This contract needs a photo or a clip. Take one or choose a file."));
    const data = new FormData();
    data.append("player_id", c.meId);
    data.append("milestone", select.value);
    data.append("value", value.value);
    if (note.value.trim()) data.append("note", note.value.trim());
    if (evidence) data.append("file", evidence, evidence.name || "evidence");
    const kind = !evidence ? "log" : evidence.type.startsWith("video/") ? "clip" : "photo";
    onPending({ milestone: Number(select.value), value: value.value, kind });
    const res = await act(submit, err, "Checking your evidence", () => api.checkin(data));
    onPending(null);
    if (res && res.event) {
      file.value = "";
      note.value = "";
      valueTouched = false;
      lastOptions = "";
      setEvidence(null);
      onRecorded(res.event);
      if (res.state) c.setState(res.state);
    }
  });

  return {
    el: form,
    update(c, events) {
      const contract = c.me.contract;
      const active = c.state.group.status === "ACTIVE";
      closed.classList.toggle("hidden", active);
      for (const control of [select, value, note, file, startCam, submit]) control.disabled = !active;
      if (!active) camera.stop();
      if (!contract) return;
      contractNow = contract;
      const unit = contract.target.unit;
      policyLine.textContent = `Evidence for your contract: ${(EVIDENCE_LABEL[contract.evidence_policy] || contract.evidence_policy).toLowerCase()}.`;
      const verified = new Set(events.filter((e) => e.state === "VERIFIED").map((e) => e.milestone));
      const options = contract.milestones.filter((m) => !verified.has(m.index));
      const signature = options.map((m) => m.index).join(",");
      if (signature !== lastOptions) {
        const keep = select.value;
        lastOptions = signature;
        fill(select, options.map((m) => h("option", { value: m.index }, `Day ${m.day}, ${m.target} ${unit}`)));
        const due = dueMilestone(contract, events);
        select.value = options.some((m) => String(m.index) === keep) && valueTouched ? keep : due ? String(due.index) : "";
        const chosen = options.find((m) => String(m.index) === select.value);
        if (!valueTouched && chosen) value.value = chosen.target;
      }
    },
  };
}

function createCamera() {
  let stream = null;
  let video = null;
  return {
    async start() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        const e = new Error("no camera API");
        e.name = "NotSupported";
        throw e;
      }
      this.stop();
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
      video = h("video", { autoplay: true, muted: true, playsinline: true });
      video.srcObject = stream;
      await video.play().catch(() => {});
      return video;
    },
    capture() {
      if (!video || !video.videoWidth) return Promise.resolve(null);
      const canvas = document.createElement("canvas");
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext("2d").drawImage(video, 0, 0);
      return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.88));
    },
    stop() {
      if (stream) stream.getTracks().forEach((t) => t.stop());
      stream = null;
      video = null;
    },
  };
}
