/* EventMate Arcade — the arcade is a live view of the real pipeline.
 *
 * Honesty rules this file keeps:
 * - a module lights only because /api/arcade/pulse says the real stage ran;
 *   the page never invents progress, and the coin drops only after the user
 *   presses BUILD MY PLAN (the same confirm the plain UI requires);
 * - the dossier shows what the API returned: validation warnings, unverified
 *   items, symbolic-check status and violations stay visible. A failing run
 *   ejects a jam slip, never a fake plan;
 * - the run replay is the recorded stage timings of THIS run, slowed so the
 *   eye can follow; the on-screen CRT and the banner both say so.
 * The 3D scene (cabinet, attendant, liquid loop, paper flight) lives in
 * arcade3d.js; this file owns conversation, API and result content.
 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = {
  csrf: "",
  messages: [],
  draft: null,
  running: false,
  pollTimer: null,
  chatWasOpen: true,
};
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ---------------------------------------------------------------- API */
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (opts.method && opts.method !== "GET") headers["X-EventMate-CSRF"] = state.csrf;
  const res = await fetch(path, { ...opts, headers });
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON body */ }
  return { ok: res.ok, status: res.status, data };
}

/* ---------------------------------------------------------------- chat */
function bubble(kind, text, notes) {
  const div = document.createElement("div");
  div.className = "bubble " + kind;
  div.textContent = text;
  if (notes && notes.length) {
    const n = document.createElement("span");
    n.className = "notes";
    n.textContent = notes.join("\n");
    div.appendChild(n);
  }
  $("#log").appendChild(div);
  $("#log").scrollTop = $("#log").scrollHeight;
  return div;
}

function fmtINR(n) {
  if (typeof n !== "number") return "—";
  let s = String(Math.round(n)), out = "", first = true;
  while (s.length > 0) {
    const take = first ? 3 : 2;
    const chunk = s.slice(-take);
    out = chunk + (out ? "," + out : "");
    s = s.slice(0, -take);
    first = false;
  }
  return "₹" + out;
}

function fieldLabel(f) {
  return {
    event_type: "event", community_subtype: "community type", guest_count: "guests",
    city: "city", event_date: "date", budget_total: "budget", dietary: "dietary",
    venue_setting: "venue setting", services: "services", reminders: "reminders",
  }[f] || f;
}

function draftValue(draft, f) {
  const v = draft[f];
  if (v === null || v === undefined || v === "") return null;
  if (f === "budget_total") return fmtINR(v);
  if (Array.isArray(v)) return v.length ? v.join(", ") : null;
  return String(v);
}

function renderReview() {
  const d = state.draft;
  if (!d) return;
  const dl = $("#review-fields");
  dl.textContent = "";
  const fields = ["event_type", "community_subtype", "guest_count", "city", "event_date",
    "budget_total", "dietary", "venue_setting", "services", "reminders"];
  for (const f of fields) {
    const val = draftValue(d, f);
    if (val === null) continue;
    const dt = document.createElement("dt");
    dt.textContent = fieldLabel(f);
    const dd = document.createElement("dd");
    dd.textContent = val;
    dl.appendChild(dt); dl.appendChild(dd);
  }
  $("#review").hidden = false;
}

function renderTurn(turn) {
  state.draft = turn.draft;
  const notes = [];
  if (turn.missing && turn.missing.length) notes.push("still missing: " + turn.missing.map(fieldLabel).join(", "));
  if (turn.flags && turn.flags.length) notes.push("please double-check: " + turn.flags.join(", "));
  if (turn.notes && turn.notes.length) notes.push(...turn.notes);

  if (turn.choices && turn.choices.length) {
    for (const ch of turn.choices) {
      const b = bubble("bot", ch.prompt, notes);
      const chips = document.createElement("div");
      chips.className = "chips";
      for (const opt of ch.options) {
        const c = document.createElement("button");
        c.className = "chip";
        c.type = "button";
        c.textContent = opt;
        c.addEventListener("click", () => { sendUser(opt); });
        chips.appendChild(c);
      }
      b.appendChild(chips);
    }
    return;
  }
  if (turn.ready) {
    bubble("bot", "That is everything I need. Check the brief below, then drop the coin.", notes);
    renderReview();
    return;
  }
  bubble("bot", turn.question || "Tell me more about the event.", notes);
}

async function sendUser(text) {
  if (state.running) return;
  state.messages.push(text);
  bubble("user", text);
  $("#msg").value = "";
  $("#review").hidden = true;
  const r = await api("/api/intake", {
    method: "POST",
    body: JSON.stringify({ messages: state.messages, draft: state.draft }),
  });
  if (!r.ok || !r.data) {
    bubble("err", (r.data && r.data.error) || "The intake call failed (" + r.status + "). Try again.");
    return;
  }
  renderTurn(r.data);
}

/* ---------------------------------------------------------------- pulse */
function applyPulse(snap) {
  if (!snap || !snap.stages) return;
  Arcade3D.livePulse(snap.stages);
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(async () => {
    try {
      const r = await api("/api/arcade/pulse");
      if (r.ok && r.data) applyPulse(r.data);
    } catch (e) { /* keep polling */ }
  }, 180);
}
function stopPolling() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
}

function setCaption(text) { $("#stage-caption").textContent = text || ""; }

/* Replay the recorded timings of this run, slowed so the eye can follow.
   The cabinet screen says REPLAY and the banner spells it out; block by block,
   each finished stage hands a continuous fill of blue liquid to the next. */
async function replayRun(snap) {
  if (!snap || !snap.stages) return;
  const ran = snap.stages.filter((s) => s.state !== "waiting");
  if (!ran.length) return;
  Arcade3D.resetWorkflow();
  Arcade3D.setCRT("REPLAY");
  $("#replay-banner").hidden = false;
  for (const st of ran) {
    const show = Math.max(380, Math.min(1400, (st.ms || 0) * 8));
    await Arcade3D.stageRun(st.name, show, st.state);
  }
  // stamp the recorded timings onto the blocks
  for (const st of ran) Arcade3D.setStageMs(st.name, st.ms);
  $("#replay-banner").hidden = true;
  setCaption("");
}

/* ---------------------------------------------------------------- dossier */
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function renderDossier(payload) {
  const plan = payload.plan;
  const inner = $("#paper-inner");
  inner.textContent = "";
  const paper = $("#paper");
  paper.classList.remove("jammed");
  const oldStamp = paper.querySelector(".stamp");
  if (oldStamp) oldStamp.remove();

  inner.appendChild(el("h1", null, "PLAN DOSSIER"));
  // same overall semantics as the classic UI: "Verified" only when nothing
  // that matters is still undecided; blocking unknowns demote the stamp.
  const blocking = (plan.validation.unverified || []).filter((u) => u.blocks_verified !== false);
  const symbolicOk = plan.symbolic && plan.symbolic.status === "valid";
  let stampText, stampCls = "stamp";
  if (blocking.length) {
    stampText = "NEEDS VERIFICATION";
    stampCls = "stamp warn";
  } else if (!symbolicOk) {
    stampText = "CHECKED";
    stampCls = "stamp warn";
  } else {
    stampText = "VERIFIED";
  }
  paper.appendChild(el("div", stampCls, stampText));
  if (blocking.length) {
    inner.appendChild(el("div", "ribbon",
      "Needs verification: " + blocking.map((u) => u.label || u.name.replace(/_/g, " ")).join("; ")));
  }
  if (!symbolicOk) {
    inner.appendChild(el("div", "ribbon",
      "Symbolic (PDDL) check: " + (plan.symbolic ? plan.symbolic.status : "unknown") +
      " - the schedule was NOT symbolically confirmed. " + (plan.symbolic && plan.symbolic.detail ? plan.symbolic.detail : "")));
  }
  inner.appendChild(el("div", "sub",
    plan.plan_id + " · v" + plan.version + " · created " + fmtTime(plan.created_at) +
    " · " + plan.change_reason));

  const facts = el("div", "factrow");
  const addFact = (k, v) => {
    const s = el("span");
    s.appendChild(el("b", null, k));
    s.appendChild(document.createTextNode(v));
    facts.appendChild(s);
  };
  addFact("EVENT", String(plan.request.event_type).replace(/_/g, " "));
  addFact("CITY", plan.request.city);
  addFact("DATE", plan.request.event_date);
  addFact("GUESTS", plan.guests.planning_headcount);
  addFact("BUDGET", fmtINR(plan.allocation.total));
  inner.appendChild(facts);

  inner.appendChild(el("h2", null, "VENDORS — lowest-cost combination that passes every hard constraint"));
  const vt = el("table");
  vt.appendChild((() => { const tr = el("tr");
    ["category", "vendor", "cost", "status", "also ran"].forEach((h, i) =>
      tr.appendChild(el("th", i === 2 ? "num" : null, h)));
    return tr; })());
  for (const sel of plan.selections) {
    const tr = el("tr");
    tr.appendChild(el("td", null, sel.category));
    const vn = el("td", null, sel.name);
    tr.appendChild(vn);
    tr.appendChild(el("td", "num", fmtINR(sel.cost)));
    tr.appendChild(el("td", null, sel.booking_status));
    const alt = sel.alternatives && sel.alternatives.length
      ? sel.alternatives.length + " alternative(s) on file"
      : (sel.no_alternative_reason || "none");
    tr.appendChild(el("td", null, alt));
    vt.appendChild(tr);
  }
  inner.appendChild(vt);

  inner.appendChild(el("h2", null, "BUDGET — exact split, sums to the rupee"));
  const bt = el("table");
  for (const line of plan.allocation.lines) {
    const tr = el("tr");
    tr.appendChild(el("td", null, line.category));
    tr.appendChild(el("td", "num", fmtINR(line.amount)));
    bt.appendChild(tr);
  }
  const ctr = el("tr");
  ctr.appendChild(el("td", null, "contingency"));
  ctr.appendChild(el("td", "num", fmtINR(plan.allocation.contingency)));
  bt.appendChild(ctr);
  const ttr = el("tr");
  ttr.appendChild(el("td", null, "selected vendor cost (estimate - nothing committed)"));
  ttr.appendChild(el("td", "num", fmtINR(plan.total_cost)));
  bt.appendChild(ttr);
  inner.appendChild(bt);

  if (payload.reality) {
    const r = payload.reality;
    const line = el("p", null, "Budget reality check: " + r.verdict + " — " + r.message);
    line.style.fontSize = "11px";
    inner.appendChild(line);
  }

  inner.appendChild(el("h2", null, "SCHEDULE — CP-SAT solved, times are event-local"));
  const st = el("table");
  for (const it of plan.items.slice(0, 12)) {
    const tr = el("tr");
    tr.appendChild(el("td", null, fmtTime(it.start)));
    tr.appendChild(el("td", null, fmtTime(it.end)));
    tr.appendChild(el("td", null, it.name));
    st.appendChild(tr);
  }
  inner.appendChild(st);

  inner.appendChild(el("h2", null, "CHECKS — re-derived from the database, not trusted from the draft"));
  const v = plan.validation;
  const passed = v.checks.filter((c) => c.passed).length;
  inner.appendChild(el("p", null, passed + " of " + v.checks.length + " hard checks passed. Symbolic (PDDL) check: " + plan.symbolic.status + "."));
  if (v.warnings && v.warnings.length) {
    const ul = el("ul");
    v.warnings.forEach((w) => ul.appendChild(el("li", "warn", "warning: " + w)));
    inner.appendChild(ul);
  }
  if (v.unverified && v.unverified.length) {
    const ul = el("ul");
    v.unverified.forEach((u) => ul.appendChild(el("li", "unverified", "needs verification: " + u.name + " — " + u.detail + " (" + u.repair + ")")));
    inner.appendChild(ul);
  }

  if (plan.request.assumptions && plan.request.assumptions.length) {
    inner.appendChild(el("h2", null, "ASSUMPTIONS — stated, never hidden"));
    const ul = el("ul");
    plan.request.assumptions.forEach((a) => ul.appendChild(el("li", null, a)));
    inner.appendChild(ul);
  }

  inner.appendChild(el("div", "foot",
    "Vendor records and prices are synthetic seed data (demo), so every cost above is an estimate from the database, not a booking or a quote; " +
    "a vendor's reply only becomes a quote once you record it yourself. This plan books nothing, commits nothing, sends nothing and pays nothing. " +
    "Confirmed bookings stay pinned when you replan."));

  const close = el("button", "paper-close", "BACK TO THE FLOOR");
  close.type = "button";
  close.addEventListener("click", hidePaper);
  inner.appendChild(close);

  showPaper(false);
}

function renderJam(payload, httpStatus) {
  const inner = $("#paper-inner");
  inner.textContent = "";
  const paper = $("#paper");
  paper.classList.add("jammed");
  const oldStamp = paper.querySelector(".stamp");
  if (oldStamp) oldStamp.remove();

  inner.appendChild(el("h1", null, "PAPER JAM"));
  paper.appendChild(el("div", "stamp jam", "NO PLAN"));
  inner.appendChild(el("div", "sub", "The machine refused to print — it could not make this brief pass the hard checks. Nothing was stored."));

  if (payload && payload.violations && payload.violations.length) {
    inner.appendChild(el("h2", null, "WHAT BLOCKED IT"));
    const ul = el("ul", "badlist");
    for (const v of payload.violations) {
      const li = el("li", null, v.constraint + ": " + v.detail);
      if (v.repair) li.appendChild(el("span", "repair", " → fix: " + v.repair));
      ul.appendChild(li);
    }
    inner.appendChild(ul);
  } else {
    inner.appendChild(el("p", null, "The server returned status " + httpStatus + " with no violations listed."));
  }
  if (payload && payload.reality && payload.reality.message) {
    inner.appendChild(el("p", null, "Budget reality: " + payload.reality.message));
  }
  inner.appendChild(el("p", null, "Rephrase or relax the brief in chat and drop another coin when it is ready."));

  const close = el("button", "paper-close", "BACK TO THE FLOOR");
  close.type = "button";
  close.addEventListener("click", hidePaper);
  inner.appendChild(close);

  showPaper(true);
}

/* The 3D sheet has already flown to the glass; the readable page is the press. */
function showPaper(jammed) {
  const layer = $("#flight-layer");
  const paper = $("#paper");
  layer.hidden = false;
  paper.classList.remove("stuck");
  void paper.offsetWidth; // restart animation
  paper.classList.add("stuck");
  Arcade3D.hideSheets();
}

function hidePaper() {
  $("#flight-layer").hidden = true;
  if (state.chatWasOpen) {
    $("#chat").classList.remove("collapsed");
    $("#chat-toggle").textContent = "HIDE";
  }
  Arcade3D.hideSheets();
  Arcade3D.resetWorkflow();
  Arcade3D.setCRT("INSERT COIN");
}

/* ---------------------------------------------------------------- run */
async function build() {
  if (state.running || !state.draft) return;
  state.running = true;
  $("#build").disabled = true;
  $("#composer").querySelector("button").disabled = true;

  const chat = $("#chat");
  state.chatWasOpen = !chat.classList.contains("collapsed");
  if (state.chatWasOpen) {
    chat.classList.add("collapsed");
    $("#chat-toggle").textContent = "CHAT";
  }

  Arcade3D.userGesture(); // audio starts here, on the user's own click
  await Arcade3D.coinInsert();
  await Arcade3D.zoomIn(); // the cabinet screen becomes the main focus
  setCaption("LIVE · modules light as the real pipeline runs");

  startPolling();
  const r = await api("/api/intake/confirm", {
    method: "POST",
    body: JSON.stringify({
      draft: state.draft,
      raw_text: state.messages.join("\n"),
      source: "rule:parser",
    }),
  });
  stopPolling();

  let snap = null;
  try { snap = (await api("/api/arcade/pulse")).data; } catch (e) { /* replay skipped */ }

  setCaption("");
  await replayRun(snap);

  const ok = !!(r.ok && r.data && r.data.plan);
  Arcade3D.setCRT(ok ? "PLAN READY" : "JAMMED");
  await Arcade3D.finishRun(ok); // pull back, shudder + cogs, sheets drop, paper flies

  if (ok) {
    renderDossier(r.data);
  } else {
    renderJam(r.data, r.status);
  }

  state.running = false;
  $("#build").disabled = false;
  $("#composer").querySelector("button").disabled = false;
}

/* ---------------------------------------------------------------- boot */
function boot() {
  const glOk = Arcade3D.init();
  $("#gl-fallback").hidden = glOk;

  $("#chat-toggle").addEventListener("click", () => {
    const chat = $("#chat");
    chat.classList.toggle("collapsed");
    $("#chat-toggle").textContent = chat.classList.contains("collapsed") ? "CHAT" : "HIDE";
  });
  const sndBtn = $("#sound-toggle");
  const paintSnd = () => { sndBtn.textContent = Arcade3D.isMuted() ? "SOUND OFF" : "SOUND ON"; };
  paintSnd();
  sndBtn.addEventListener("click", () => {
    Arcade3D.setMuted(!Arcade3D.isMuted());
    if (!Arcade3D.isMuted()) Arcade3D.userGesture();
    paintSnd();
  });

  $("#composer").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const t = $("#msg").value.trim();
    if (t) sendUser(t).catch((e) => bubble("err", "Network error: " + e.message));
  });
  $("#build").addEventListener("click", () => build().catch((e) => {
    state.running = false;
    stopPolling();
    $("#replay-banner").hidden = true;
    $("#build").disabled = false;
    Arcade3D.setCRT("INSERT COIN");
    Arcade3D.zoomOut();
    bubble("err", "The run failed before a result came back: " + e.message);
  }));

  (async () => {
    try {
      const s = await api("/api/session");
      state.csrf = s.data.csrf;
      $("#conn").textContent = "online · " + s.data.today;
      $("#conn").className = "ok";
    } catch (e) {
      $("#conn").textContent = "offline";
      $("#conn").className = "bad";
    }
    if (glOk) Arcade3D.powerOn(); // the attendant walks over and turns the cabinet on
    bubble("bot",
      "Evening. The attendant is waking the cabinet — the loop on its screen is the real planning engine.\n" +
      "Describe your event (guests, city, date, budget; catering / photography / venue if you want them) " +
      "and I will check the brief with you. When you press BUILD MY PLAN, the coin drops, the camera moves in, " +
      "and each block lights as the real work passes through it. The result prints as a dossier you can read.");
    $("#msg").focus();
  })();
}

document.addEventListener("DOMContentLoaded", boot);
