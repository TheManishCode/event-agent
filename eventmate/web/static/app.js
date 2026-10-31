/* EventMate UI. No framework, no innerHTML: every dynamic string goes through textContent. */
(() => {
  "use strict";

  const EXAMPLES = [
    ["Engagement, Pune", "Plan a 120-person engagement party in Pune on December 12 under INR 200,000. Vegetarian catering, indoor venue, photographer, and reminders for invitations and vendor confirmation."],
    ["Small birthday", "Plan a 40 guests birthday in Pune on December 12 under INR 90,000 with catering and a photographer."],
    ["Too tight", "Plan a 120 guests party in Pune on December 12 under INR 60,000 with vegetarian catering."],
  ];
  const TABS = [["overview", "Overview"], ["ideas", "Ideas"], ["timeline", "Timeline"], ["vendors", "Vendors"], ["budget", "Budget"], ["community", "Community"], ["talk", "Vendor chats"], ["changes", "Changes"], ["audit", "Audit log"], ["ask", "Ask"]];
  const AGENTS = { allocate_budget: "budget", track_guests: "guest", source_vendor: "vendor", build_schedule: "scheduler", schedule_reminder: "scheduler", build_plan_model: "planner", validate_plan: "validator", explain_plan: "explainer" };
  const state = { csrf: "", today: "", plans: [], planId: null, version: null, plan: null, versions: [], tab: "overview", tasks: null, diff: null, decisions: null, ask: {}, roles: ["organizer"], attention: 0, senderName: "", prefillQuote: null, talkTimer: null, ideaPrefs: "" };

  // ------------------------------------------------------------ helpers
  const $ = (sel, root = document) => root.querySelector(sel);
  function el(tag, props = {}, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k === "on") for (const [ev, fn] of Object.entries(v)) n.addEventListener(ev, fn);
      else if (k === "style") for (const [sk, sv] of Object.entries(v)) n.style[sk] = sv; // CSSOM: allowed under CSP
      else n.setAttribute(k, v === true ? "" : String(v));
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return n;
  }
  const clear = (n) => { while (n.firstChild) n.removeChild(n.firstChild); return n; };
  const inr = (n) => "INR " + Number(n).toLocaleString("en-US");
  const parseDT = (s) => new Date(s); // naive ISO => local wall-clock time
  const fmtDate = (d) => d.toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "short", year: "numeric" });
  const fmtTime = (d) => d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  const fmtDT = (s) => { const d = parseDT(s); return `${fmtDate(d)}, ${fmtTime(d)}`; };
  const titleCase = (s) => s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  const daysBetween = (a, b) => Math.round((b - a) / 86400000);

  let toastTimer;
  function toast(msg, ms = 3200) {
    const t = $("#toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, ms);
  }

  async function api(path, opts = {}) {
    const headers = { Accept: "application/json" };
    const init = { method: opts.method || "GET", headers, credentials: "same-origin" };
    headers["X-EventMate-CSRF"] = state.csrf;
    if (opts.body !== undefined) { headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
    let res;
    try { res = await fetch(path, init); } catch (e) { throw new Error("Cannot reach the EventMate server."); }
    let data = null;
    try { data = await res.json(); } catch (e) { /* non-JSON error body */ }
    return { status: res.status, ok: res.ok, data };
  }

  // -------------------------------------------------------------- theme
  function applyTheme(t) { document.documentElement.setAttribute("data-theme", t); try { localStorage.setItem("em-theme", t); } catch (e) { /* private mode */ } }
  function initTheme() {
    let t = null; try { t = localStorage.getItem("em-theme"); } catch (e) { /* ignore */ }
    applyTheme(t || (window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));
    $("#theme-toggle").addEventListener("click", () => applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark"));
  }

  // ------------------------------------------------------------ rail
  function renderExamples() {
    const box = clear($("#examples"));
    for (const [label, text] of EXAMPLES) box.append(el("button", { class: "chip", type: "button", text: label, on: { click: () => { $("#request-text").value = text; $("#request-text").focus(); } } }));
  }

  function renderPlanList() {
    const ul = clear($("#plan-list"));
    $("#plan-list-empty").hidden = state.plans.length > 0;
    for (const p of state.plans) {
      ul.append(el("li", {}, el("button", { class: "plan-item", type: "button", "aria-current": p.plan_id === state.planId ? "true" : "false", on: { click: () => selectPlan(p.plan_id) } },
        el("b", { text: `${titleCase(p.event_type)} in ${p.city}` }),
        el("span", { text: `${p.event_date} · ${p.guest_count} guests · v${p.version} · ${inr(p.total_cost)} of ${inr(p.budget_total)}` }))));
    }
  }

  function showViolations(box, title, violations, problems) {
    clear(box);
    box.append(el("b", { text: title }));
    const ul = el("ul");
    for (const p of problems || []) ul.append(el("li", { text: p }));
    for (const v of violations || []) ul.append(el("li", {}, el("b", { text: v.constraint }), ": " + v.detail, el("div", { class: "repair", text: "Repair: " + v.repair })));
    box.append(ul);
    box.hidden = false;
  }

  async function onSubmitPlan(ev) {
    ev.preventDefault();
    const text = $("#request-text").value.trim();
    const errs = $("#plan-errors"); errs.hidden = true;
    if (!text) { showViolations(errs, "Describe the event first.", [], []); return; }
    const btn = $("#plan-button"); btn.disabled = true; btn.textContent = "Planning, solving and validating…";
    try {
      const body = { request: text }; const gl = $("#guest-list").value; if (gl) body.guest_list_id = gl;
      const r = await api("/api/plans", { method: "POST", body });
      if (r.status === 201) {
        state.tasks = r.data.tasks; await refreshList(); await selectPlan(r.data.plan.plan_id, null, "overview"); toast("Plan validated and saved as version 1.");
        showReality(errs, r.data.reality);
      } else if (r.status === 422 && r.data && r.data.problems) showViolations(errs, "I could not parse that request:", [], r.data.problems);
      else if (r.status === 422 && r.data && r.data.violations) { showViolations(errs, "No plan can satisfy every hard constraint:", r.data.violations, []); showReality(errs, r.data.reality); offerTradeoffs(errs, body); }
      else showViolations(errs, (r.data && r.data.error) || "Something went wrong.", [], []);
    } catch (e) { showViolations(errs, e.message, [], []); }
    finally { btn.disabled = false; btn.textContent = "Build validated plan"; }
  }

  // Budget reality check: always an estimate, always labelled.
  function showReality(box, rc) {
    if (!rc) return;
    const kind = { comfortable: "ok", tight: "warn", contingency_breach: "warn", unrealistic: "err", unknown: "warn" }[rc.verdict] || "warn";
    if (rc.verdict === "comfortable" && box.hidden) { box.className = "notice ok"; clear(box); } else if (box.hidden) { box.className = "notice " + kind; clear(box); }
    const note = el("div", { class: "reality " + kind, id: "reality" }, el("b", { text: "Budget reality check: " }), rc.message, el("div", { class: "muted", text: rc.basis }));
    box.append(note); box.hidden = false;
  }

  // ---------------------------------------------------------- trade-offs
  function offerTradeoffs(box, body) {
    const holder = el("div", { class: "tradeoffs" });
    const btn = el("button", { class: "btn wide", id: "tradeoff-button", type: "button", text: "Show verified trade-offs", on: { click: async () => {
      btn.disabled = true; btn.textContent = "Checking every option against the full pipeline…";
      const r = await api("/api/tradeoffs", { method: "POST", body });
      btn.remove();
      if (!r.ok) { holder.append(el("div", { class: "notice err", text: (r.data && r.data.error) || "Could not compute trade-offs." })); return; }
      renderTradeoffs(holder, r.data, body);
    } } });
    box.append(btn, holder);
  }

  function renderTradeoffs(holder, report, body) {
    if (report.status !== "options") {
      holder.append(el("div", { class: "notice warn", text: report.status === "not_needed" ? "This request plans fine; nothing to trade off." : "No verified trade-off exists: " + report.violations.map((v) => v.detail + " " + v.repair).join(" ") }));
      return;
    }
    holder.append(el("p", { class: "muted", text: "Ranked by least harm to your preferences and least extra spend. Each one was built and validated as a full plan; nothing is applied until you choose." }));
    for (const o of report.options) {
      const harm = Math.round(o.preference_harm * 100);
      holder.append(el("article", { class: "option" },
        el("h3", { text: o.title }),
        el("p", { text: o.detail }),
        el("div", { class: "badges" },
          badge(`Preference impact ${harm}%`, harm >= 50 ? "err" : harm >= 15 ? "warn" : "ok"),
          badge(o.extra_spend ? `Costs ${inr(o.extra_spend)} more` : "No extra spend", o.extra_spend ? "warn" : "ok"),
          (o.needs_verification && o.needs_verification.length)
            ? badge("Plan built, needs verification: " + o.needs_verification.join("; "), "warn")
            : badge("Verified: full plan built", "ok")),
        el("button", { class: "btn primary", type: "button", text: "Apply this option", on: { click: (ev) => applyOption(o, body, ev.currentTarget) } })));
    }
  }

  async function applyOption(option, body, btn) {
    btn.disabled = true; btn.textContent = "Planning…";
    const payload = { option }; if (body.guest_list_id) payload.guest_list_id = body.guest_list_id;
    const r = await api("/api/tradeoffs/apply", { method: "POST", body: payload });
    if (r.status === 201) {
      $("#plan-errors").hidden = true; state.tasks = r.data.tasks; await refreshList(); await selectPlan(r.data.plan.plan_id, null, "overview"); toast("Trade-off applied; plan validated and saved.");
    } else { btn.disabled = false; btn.textContent = "Apply this option"; toast((r.data && (r.data.error || (r.data.violations && r.data.violations[0] && r.data.violations[0].detail))) || "That option no longer plans.", 6000); }
  }

  // ------------------------------------------------------------ data flow
  async function refreshList() { const r = await api("/api/plans"); state.plans = r.ok ? r.data : []; renderPlanList(); }

  async function selectPlan(id, version = null, tab = null) {
    const q = version ? `?version=${version}` : "";
    const [p, vs] = await Promise.all([api(`/api/plans/${encodeURIComponent(id)}${q}`), api(`/api/plans/${encodeURIComponent(id)}/versions`)]);
    if (!p.ok) { toast((p.data && p.data.error) || "Could not load that plan."); return; }
    state.planId = id; state.plan = p.data; state.version = p.data.version; state.versions = vs.ok ? vs.data : [];
    state.diff = null; state.decisions = null;
    state.exploring = false; $("#explore-view").hidden = true;
    if (state.plan.version !== 1) state.tasks = null; // the agent pipeline view belongs to the initial build
    if (tab) state.tab = tab;
    renderPlanList(); render();
  }

  // ------------------------------------------------------------- render
  function render() {
    const p = state.plan;
    const exploring = !$("#explore-view").hidden;
    $("#empty-state").hidden = !!p || exploring; $("#plan-view").hidden = !p || exploring;
    const sel = $("#version-select");
    if (!p) { sel.hidden = true; return; }
    clear(sel); sel.hidden = state.versions.length < 2;
    for (const v of state.versions) sel.append(el("option", { value: v.version, text: `Version ${v.version}`, selected: v.version === p.version }));
    renderHero(); renderTabs(); renderPanels(); refreshAttention();
  }

  // One reading of "is this plan fully verified": the server's current_verification, else what the stored report says.
  function verification(p) {
    if (p && p.current_verification) return p.current_verification;
    const blocking = ((p && p.validation && p.validation.unverified) || []).filter((u) => u.blocks_verified !== false);
    return { state: blocking.length ? "needs_verification" : "verified", label: blocking.length ? "Needs verification: " + blocking.map((u) => u.label || u.name).join("; ") : "Verified", items: blocking };
  }

  function badge(text, kind) { return el("span", { class: "badge " + (kind || "") }, el("i", { class: "dot" }), text); }

  function renderHero() {
    const p = state.plan, r = p.request, hero = clear($("#hero"));
    const passed = p.validation.checks.filter((c) => c.passed).length, total = p.validation.checks.length;
    const days = daysBetween(new Date(state.today + "T00:00:00"), new Date(r.event_date + "T00:00:00"));
    const used = p.total_cost / r.budget_total;
    const confirmed = p.selections.filter((s) => s.booking_status === "confirmed").length;
    hero.append(
      el("div", { class: "hero-top" },
        el("div", {}, el("h1", { text: `${titleCase(r.event_type)} in ${r.city}` }), el("div", { class: "sub", text: `${fmtDate(new Date(r.event_date + "T00:00:00"))} · ${r.guest_count} guests · plan ${p.plan_id} · version ${p.version}${p.parent_version ? " (from v" + p.parent_version + ")" : ""}` })),
        el("div", { class: "badges" },
          verification(p).state === "verified"
            ? badge(`${passed}/${total} hard constraints passed`, passed === total ? "ok" : "err")
            : badge(verification(p).label, "warn"),
          verification(p).state === "verified" ? null : badge(`${passed}/${total} decidable checks passed`, passed === total ? "ok" : "err"),
          badge(`PDDL ${p.symbolic.status}`, p.symbolic.status === "valid" ? "ok" : "warn"),
          badge(`CP-SAT ${p.solution.status}`, "acc"),
          badge(`${confirmed} confirmed booking${confirmed === 1 ? "" : "s"}`, confirmed ? "ok" : ""))),
      el("div", { class: "kpis" },
        kpi("Vendors total", inr(p.total_cost), `${Math.round(used * 100)}% of ${inr(r.budget_total)}`, used, used > 0.9 ? "warn" : ""),
        kpi("Left in budget", inr(r.budget_total - p.total_cost), `contingency reserve ${inr(p.allocation.contingency)}`),
        kpi("Days to event", days >= 0 ? String(days) : "past", `${p.items.length} scheduled tasks`),
        kpi("Guests", String(r.guest_count), `${p.guests.confirmed} confirmed · ${p.guests.pending} pending · ${p.guests.declined} declined`)),
      el("div", { class: "muted", text: "Reason for this version: " + p.change_reason }));
  }
  function kpi(label, value, note, ratio, kind) {
    return el("div", { class: "kpi" }, el("div", { class: "label", text: label }), el("div", { class: "value", text: value }), el("div", { class: "note", text: note }),
      ratio === undefined ? null : el("div", { class: "meter", role: "img", "aria-label": `${Math.round(ratio * 100)} percent` }, el("i", { class: kind || "", style: { width: Math.min(100, Math.round(ratio * 100)) + "%" } })));
  }

  function renderTabs() {
    const nav = clear($("#tabs"));
    if (state.tab === "community" && state.plan && state.plan.request.event_type !== "community") state.tab = "overview";
    for (const [id, label0] of TABS) if (!(id === "community" && state.plan && state.plan.request.event_type !== "community")) nav.append(el("button", { class: "tab", role: "tab", id: "tab-" + id, "aria-selected": String(state.tab === id), "aria-controls": "panel-" + id, type: "button", text: id === "talk" && state.attention ? `${label0} (${state.attention})` : label0, on: { click: () => { state.tab = id; renderTabs(); renderPanels(); } } }));
  }

  function renderPanels() {
    clearInterval(state.talkTimer);
    const host = clear($("#panels"));
    const builders = { overview: panelOverview, ideas: panelIdeas, community: panelCommunity, timeline: panelTimeline, vendors: panelVendors, budget: panelBudget, talk: panelTalk, changes: panelChanges, audit: panelAudit, ask: panelAsk };
    const panel = el("div", { class: "panel", role: "tabpanel", id: "panel-" + state.tab, "aria-labelledby": "tab-" + state.tab });
    host.append(panel);
    builders[state.tab](panel);
  }

  // ------------------------------------------------------------- assistant
  // Answers come with their sources, a "how I answered" trace, and any change as a proposal that only a click here can apply.
  const ROUTE_LABEL = { direct: "answered from your plan, no AI needed", small: "small AI model", large: "large AI model", annotation: "approved answer, no AI needed", moderated: "blocked by policy" };
  const SUGGESTED = { organizer: ["How much is left in the budget?", "Is my budget realistic?", "What should I ask the caterer about vegetarian food?"], other: ["When is the event?", "Where is the event?"] };
  function panelAsk(root) {
    const log = (state.ask[state.planId] = state.ask[state.planId] || []);
    const thread = el("div", { class: "ask-thread", role: "log", "aria-live": "polite" });
    const pending = el("section", { class: "card", id: "pending-approvals", hidden: true }, el("h2", { text: "Waiting for your approval" }));
    const input = el("input", { class: "input", id: "ask-input", type: "text", maxlength: "1000", placeholder: "Ask about this plan: budget, vendors, dietary needs, how EventMate works" });
    const send = el("button", { class: "btn primary", type: "submit", text: "Ask" });
    const form = el("form", { class: "chat-form", autocomplete: "off", on: { submit: async (ev) => {
      ev.preventDefault(); const q = input.value.trim(); if (!q) return; input.value = ""; send.disabled = true;
      log.push({ me: q }); drawAsk(thread, log);
      const r = await api("/api/assistant/ask", { method: "POST", body: { plan_id: state.planId, question: q } });
      send.disabled = false;
      log.push(r.ok ? { ai: r.data } : { err: (r.data && r.data.error) || "I could not answer that." });
      drawAsk(thread, log); loadPending(pending);
    } } }, el("label", { class: "sr", for: "ask-input", text: "Ask the assistant" }), input, send);
    const ideas = el("div", { class: "chips" }, ...(state.roles.includes("organizer") ? SUGGESTED.organizer : SUGGESTED.other).map((q) => el("button", { class: "chip", type: "button", text: q, on: { click: () => { input.value = q; input.focus(); } } })));
    const approved = el("section", { class: "card", id: "approved-answers", hidden: true }, el("h2", { text: "Approved answers" }));
    root.append(el("section", { class: "card" }, el("h2", { text: "Ask EventMate" }),
      el("p", { class: "muted", text: "Answers use only this plan and EventMate's own documents, and say where they came from. Nothing changes unless you approve it." }), thread, form, ideas), pending, approved);
    drawAsk(thread, log); loadPending(pending); loadApproved(approved);
  }

  function drawAsk(thread, log) {
    clear(thread);
    for (const m of log) {
      if (m.me) thread.append(el("div", { class: "bubble me", text: m.me }));
      else if (m.err) thread.append(el("div", { class: "bubble note", text: m.err }));
      else thread.append(answerBubble(m.ai));
    }
    thread.scrollTop = thread.scrollHeight;
  }

  function answerBubble(a) {
    const box = el("div", { class: "bubble ai answer" }, el("div", { text: a.answer }));
    const meta = el("div", { class: "answer-meta" });
    if (a.citations.length) meta.append(el("span", { class: "muted", text: "Sources:" }), ...a.citations.map((c) => el("span", { class: "chip", text: c })));
    meta.append(el("span", { class: "badge " + (a.outcome === "answered" ? "ok" : "warn"), text: a.outcome }), el("span", { class: "muted", text: ROUTE_LABEL[a.route] + (a.cache !== "miss" ? ", reused" : "") }));
    box.append(meta);
    if (a.degraded_reason) box.append(el("div", { class: "muted", text: "Why this is a fallback: " + a.degraded_reason }));
    for (const p of a.proposals) box.append(el("div", { class: "muted", text: "Filed for your approval: " + p.summary }));
    const note = el("span", { class: "muted" });
    const fb = (value) => async () => { const r = await api(`/api/assistant/traces/${a.trace_id}/feedback`, { method: "POST", body: { value } }); note.textContent = r.ok ? "Thanks, noted." : "Could not save that."; };
    const actions = el("div", { class: "answer-meta" }, el("button", { class: "btn small ghost", type: "button", text: "Helpful", on: { click: fb(1) } }), el("button", { class: "btn small ghost", type: "button", text: "Not helpful", on: { click: fb(-1) } }));
    if (state.roles.includes("organizer") && a.outcome === "answered" && a.route !== "annotation") {
      actions.append(el("button", { class: "btn small ghost", type: "button", text: "Save as approved answer", on: { click: async () => {
        const r = await api("/api/assistant/annotations", { method: "POST", body: { trace_id: a.trace_id } });
        note.textContent = r.ok ? "Saved. Similar questions will reuse it while the plan stays as it is." : ((r.data && r.data.error) || "Could not save that.");
        const card = $("#approved-answers"); if (card) loadApproved(card);
      } } }));
    }
    actions.append(note); box.append(actions);
    const why = el("details", { class: "why-trace" }, el("summary", { text: "How I answered" }));
    let loaded = false;
    why.addEventListener("toggle", async () => {
      if (!why.open || loaded) return; loaded = true;
      const r = await api("/api/assistant/traces/" + a.trace_id);
      if (!r.ok) { why.append(el("p", { class: "muted", text: "The trace is only visible to the organiser." })); return; }
      why.append(el("ol", { class: "trace" }, ...r.data.steps.map((s) => el("li", { class: s.ok ? "" : "bad" }, el("b", { text: s.kind + ": " + s.name }), s.detail ? " " + s.detail : "", s.ms ? el("span", { class: "muted", text: " " + s.ms + " ms" }) : null))),
        el("p", { class: "muted", text: `Estimated tokens in/out: ${r.data.tokens_in_est}/${r.data.tokens_out_est}. Prompt ${r.data.prompt_sha256.slice(0, 8)}, knowledge ${r.data.kb_version.slice(0, 8)}.` }));
    });
    box.append(why);
    return box;
  }

  async function loadApproved(card) {
    const r = await api(`/api/assistant/annotations?plan_id=${state.planId}`);
    for (const n of [...card.children].slice(1)) n.remove();
    const items = r.ok ? r.data : [];
    card.hidden = items.length === 0;
    for (const a of items) {
      card.append(el("div", { class: "proposal" }, el("b", { text: a.question }), el("div", { text: a.answer }),
        el("div", { class: "actions" }, el("span", { class: "badge " + (a.stale ? "warn" : "ok"), text: a.stale ? "stale: the plan changed, not used" : "in use" }),
          el("button", { class: "btn small", type: "button", text: "Remove", on: { click: async () => { await api(`/api/assistant/annotations/${a.id}/delete`, { method: "POST", body: { plan_id: state.planId } }); loadApproved(card); } } }))));
    }
  }

  async function loadPending(card) {
    const r = await api(`/api/assistant/approvals?plan_id=${state.planId}`);
    for (const n of [...card.children].slice(1)) n.remove();
    const items = r.ok ? r.data : [];
    card.hidden = items.length === 0;
    for (const p of items) {
      const note = el("span", { class: "muted" });
      const decide = (verdict) => async () => {
        const d = await api(`/api/assistant/approvals/${p.id}/${verdict}`, { method: "POST", body: {} });
        if (!d.ok) { note.textContent = (d.data && d.data.error) || "Could not record that."; return; }
        toast(d.data.status === "approved" ? "Done: " + d.data.result : "Proposal " + d.data.status + (d.data.result ? ": " + d.data.result : ""), 5000);
        await refreshList(); await selectPlan(state.planId, null, "ask");
      };
      card.append(el("div", { class: "proposal" }, el("b", { text: p.summary }), el("div", { class: "muted", text: `Proposed by ${p.proposer}. Nothing happens until you approve.` }),
        el("div", { class: "actions" }, el("button", { class: "btn primary small", type: "button", text: "Approve", on: { click: decide("approve") } }), el("button", { class: "btn small", type: "button", text: "Reject", on: { click: decide("reject") } }), note)));
    }
  }

  // ----------------------------------------------------------- overview
  // Advisory checklists from the rule packs: questions to ask, never constraints. Unreviewed packs say so.
  async function renderAdvisories(card) {
    const r = await api(`/api/plans/${state.planId}/advisories`);
    if (!r.ok || !r.data.length) { card.hidden = true; return; }
    const list = el("ul", { class: "advisories" });
    for (const a of r.data) list.append(el("li", {}, el("b", { text: a.title }), el("div", { class: "muted", text: a.detail }), el("div", { class: "muted mono", text: `${a.pack_id} v${a.pack_version} · ${a.reviewed ? "reviewed" : "unreviewed draft"}` })));
    card.append(el("p", { class: "muted", text: "Advisory only: questions to put to your vendors or host. They do not change the plan." }), list);
    card.hidden = false;
  }
  function panelOverview(root) {
    const p = state.plan;
    const adv = el("section", { class: "card", id: "advisories", hidden: true }, el("h2", { text: "Things to confirm" }));
    root.append(adv);
    renderAdvisories(adv);
    const wx = el("section", { class: "card", id: "weather" }, el("h2", { text: "Weather on the day" }), el("p", { class: "muted", text: "Loading…" }));
    root.append(wx);
    renderWeather(wx);
    if (state.tasks && p.version === 1) {
      const pipe = el("div", { class: "pipeline" });
      for (const t of state.tasks) pipe.append(el("span", { class: "step " + t.status, title: `${t.id} handled by the ${t.agent} agent`, text: `${t.agent}: ${titleCase(t.id.split(":")[0])}${t.id.endsWith(":event") ? "" : " · " + titleCase(t.id.split(":")[1])}` }));
      root.append(el("section", { class: "card" }, el("h2", { text: "Agent pipeline" }), pipe));
    }
    const groups = {};
    for (const c of p.validation.checks) { const k = c.name.includes(":") ? c.name.split(":")[0] : "plan"; (groups[k] = groups[k] || []).push(c); }
    const checks = el("div", { class: "checks" });
    for (const [g, list] of Object.entries(groups)) {
      checks.append(el("details", { class: "fold", open: g === "plan" }, el("summary", { text: `${titleCase(g)} · ${list.filter((c) => c.passed).length}/${list.length}` }),
        ...list.map((c) => el("div", { class: "check" + (c.passed ? "" : " fail") }, el("span", { class: "tick", text: c.passed ? "✓" : "✗" }), el("div", {}, el("code", { text: c.name.split(":").pop() }), " ", el("span", { class: "why", text: c.detail }))))));
    }
    root.append(el("div", { class: "grid2" },
      el("section", { class: "card" }, el("h2", { text: verification(p).state === "verified" ? "Why this plan is valid" : "What was checked, and what is still open" }), el("p", { class: "muted", text: "Re-derived from the vendor database, independent of the planner and solver." }),
        verification(p).state === "verified" ? null : el("div", { id: "ov-verification", class: "notice warn", text: `${verification(p).label}. A provisional plan: every check that could be decided passed. ${verification(p).items.map((i) => i.repair).join(" ")}` }), checks),
      el("section", { class: "card" }, el("h2", { text: "Assumptions and warnings" }),
        ...(p.request.assumptions.length || p.warnings.length ? [...p.request.assumptions.map((a) => el("div", { class: "notice", text: "Assumption: " + a })), ...p.warnings.map((w) => el("div", { class: "notice warn", text: w }))] : [el("p", { class: "muted", text: "None." })]),
        el("h2", { text: "Symbolic check" }), el("p", { class: "muted", text: `${p.symbolic.detail}${p.symbolic.plan_actions.length ? " (" + p.symbolic.plan_actions.length + " steps)" : ""}` }),
        el("div", { class: "actions" }, el("button", { class: "btn", type: "button", text: "Re-validate against current data", on: { click: revalidate } }), el("button", { class: "btn ghost", type: "button", text: "Copy text summary", on: { click: copySummary } })))));
  }
  // Forecast from OpenWeather. Beyond its horizon (about five days) it says so; nothing is estimated.
  async function renderWeather(card) {
    const st = await api("/api/weather/status");
    for (const n of [...card.children].slice(1)) n.remove();
    if (st.ok && !st.data.configured) { card.append(el("p", { class: "muted", id: "weather-msg", text: "Weather is not configured: add OPENWEATHER_API_KEY to the .env file." })); return; }
    const r = await api(`/api/plans/${state.planId}/weather-advice`);
    if (!r.ok) { card.append(el("p", { class: "muted", id: "weather-msg", text: (r.data && r.data.error) || "The forecast could not be loaded." })); return; }
    const w = r.data.forecast, advice = r.data.items;
    if (!w.slots.length) { card.append(el("p", { class: "muted", id: "weather-msg", text: `${w.resolved_place}: ${w.note || "no forecast yet."}` })); card.append(el("p", { class: "muted", text: w.attribution })); return; }
    card.append(
      el("p", { id: "weather-msg", text: `${w.resolved_place}: ${w.temp_min}°C to ${w.temp_max}°C, ${w.conditions || "no description"}; chance of rain up to ${Math.round(w.max_rain_chance * 100)}%.` }),
      ...advice.map((a) => el("div", { class: "notice" + (a.level === "warn" ? " warn" : ""), text: `${a.title}. ${a.detail}` })),
      el("table", { class: "tbl" }, el("thead", {}, el("tr", {}, el("th", { text: "Time" }), el("th", { class: "num", text: "°C" }), el("th", { class: "num", text: "Rain" }), el("th", { text: "Conditions" }))),
        el("tbody", {}, ...w.slots.map((x) => el("tr", {}, el("td", { text: x.local_time.slice(11, 16) }), el("td", { class: "num", text: String(Math.round(x.temp_c)) }), el("td", { class: "num", text: Math.round(x.rain_chance * 100) + "%" }), el("td", { text: x.description || "" }))))),
      el("p", { class: "muted", text: w.attribution }));
  }
  async function revalidate() {
    const r = await api(`/api/plans/${state.planId}/validate?version=${state.version}`);
    toast(r.ok ? (r.data.passed ? `Still valid: ${r.data.checks.length} checks passed against current data.` : `No longer valid: ${r.data.violations.length} constraint(s) fail now.`) : "Validation failed to run.", 4500);
  }
  async function copySummary() {
    const r = await api(`/api/plans/${state.planId}/explanation?version=${state.version}`);
    if (!r.ok) return toast("Could not build the summary.");
    try { await navigator.clipboard.writeText(r.data.markdown); toast("Summary copied."); } catch (e) { toast("Copy is blocked by the browser."); }
  }

  // ----------------------------------------------------------- timeline
  function panelTimeline(root) {
    const items = state.plan.items.map((i) => ({ ...i, s: parseDT(i.start), e: parseDT(i.end) }));
    const pre = items.filter((i) => i.group === "pre_event"), day = items.filter((i) => i.group === "event_day");
    const legend = el("div", { class: "legend" }); for (const [k, c] of [["organizer", "--organizer"], ["venue", "--venue"], ["caterer", "--caterer"], ["photographer", "--photographer"]]) legend.append(el("span", {}, el("i", { style: { background: `var(${c})` } }), titleCase(k)));
    if (pre.length) {
      const t0 = Math.min(...pre.map((i) => i.s)), t1 = Math.max(...pre.map((i) => +i.e)) + 86400000;
      const g = el("div", { class: "gantt" });
      const ticks = el("div", { class: "g-ticks" });
      const spanDays = Math.max(1, Math.ceil((t1 - t0) / 86400000)), step = Math.max(1, Math.ceil(spanDays / 8));
      for (let d = 0; d <= spanDays; d += step) ticks.append(el("span", { text: new Date(t0 + d * 86400000).toLocaleDateString("en-GB", { day: "2-digit", month: "short" }), style: { left: (d / spanDays) * 100 + "%" } }));
      g.append(el("div", { class: "g-axis" }, el("span"), ticks));
      for (const i of pre) g.append(ganttRow(i, t0, t1));
      root.append(el("section", { class: "card" }, el("h2", { text: "Before the event" }), legend, g));
    }
    if (day.length) {
      const d0 = new Date(day[0].s); d0.setHours(Math.min(...day.map((i) => i.s.getHours())), 0, 0, 0);
      const t0 = +d0, lastEnd = Math.max(...day.map((i) => +i.e)), t1 = Math.ceil((lastEnd - t0) / 3600000) * 3600000 + t0;
      const g = el("div", { class: "gantt" }), ticks = el("div", { class: "g-ticks" }), hours = Math.round((t1 - t0) / 3600000);
      for (let h = 0; h <= hours; h += Math.max(1, Math.ceil(hours / 10))) ticks.append(el("span", { text: fmtTime(new Date(t0 + h * 3600000)), style: { left: (h / hours) * 100 + "%" } }));
      g.append(el("div", { class: "g-axis" }, el("span"), ticks));
      for (const i of day) g.append(ganttRow(i, t0, t1));
      root.append(el("section", { class: "card" }, el("h2", { text: "Event day · " + fmtDate(day[0].s) }), g));
    }
  }
  function ganttRow(i, t0, t1) {
    const span = t1 - t0, left = ((+i.s - t0) / span) * 100, width = Math.max(0.6, ((+i.e - +i.s) / span) * 100);
    const when = `${fmtDT(i.start)} to ${fmtTime(i.e)}`;
    return el("div", { class: "g-row" },
      el("div", { class: "g-label", title: i.name }, i.name, el("small", { text: (i.group === "pre_event" ? i.s.toLocaleDateString("en-GB", { day: "2-digit", month: "short" }) + ", " : "") + fmtTime(i.s) + (i.resource ? " · " + i.resource : "") })),
      el("div", { class: "g-track" }, el("div", { class: "g-bar " + (i.resource || "organizer"), title: `${i.name}: ${when}`, style: { left: left + "%", width: width + "%" } })));
  }

  // ------------------------------------------------------------ vendors
  function panelVendors(root) {
    const p = state.plan;
    for (const s of p.selections) {
      const confirmed = s.booking_status === "confirmed";
      const alts = el("ul", { class: "alts" });
      for (const a of s.alternatives) alts.append(el("li", { class: "alt" }, el("b", { text: a.option }), el("span", { class: "tag " + (a.outcome === "rejected" ? "rej" : "eli"), text: a.outcome === "rejected" ? "rejected" : "eligible" }), el("div", { class: "muted", text: (a.outcome === "rejected" ? "" : a.outcome + ". ") + a.reason_not_chosen })));
      if (!s.alternatives.length) alts.append(el("li", { class: "alt muted", text: "No alternative: " + (s.no_alternative_reason || "") }));
      const why = el("div", { class: "why", hidden: true });
      root.append(el("section", { class: "card vendor " + s.category },
        el("div", { class: "vendor-head" },
          el("div", {}, el("div", { class: "cat", text: s.category }), el("h3", { text: s.name }), el("div", { class: "muted mono", text: s.vendor_id + " · " + s.source })),
          el("div", { style: { textAlign: "right" } }, el("div", { class: "price mono", text: inr(s.cost) }), el("span", { class: "conf-badge", "data-vendor": s.vendor_id }, badge(confirmed ? "Confirmed by you" : "Proposed", confirmed ? "ok" : "")))),
        el("p", { text: s.justification }),
        why,
        el("details", { class: "fold", open: true }, el("summary", { text: `Alternatives considered (${s.alternatives.length})` }), alts),
        el("div", { class: "actions" },
          el("button", { class: "btn small", type: "button", text: "Why this vendor?", on: { click: () => toggleWhy(s, why) } }),
          confirmed ? null : el("button", { class: "btn small", type: "button", text: "I confirmed this booking", on: { click: () => confirmBooking(s) } }),
          el("button", { class: "btn small danger", type: "button", text: "Vendor cancelled…", on: { click: () => openReplan("vendor_cancellation", s.vendor_id) } }))));
    }
    root.append(el("div", { class: "actions" }, el("button", { class: "btn", type: "button", text: "Something else changed…", on: { click: () => openReplan("budget_change") } })));
    markVendorConfirmations(root);
  }
  // "Why?": the hard constraints the database check ran for this pick, from the decision log (newest matching entry wins).
  async function toggleWhy(s, box) {
    if (!box.hidden) { box.hidden = true; return; }
    if (!state.decisions) { const r = await api(`/api/plans/${state.planId}/decisions?version=${state.version}`); state.decisions = r.ok ? r.data : []; }
    const found = state.decisions.filter((e) => e.decision_type === "recommendation" && e.summary.endsWith("top choice " + s.name)).pop();
    clear(box);
    if (!found) box.append(el("p", { class: "muted", text: "No decision-log entry was found for this pick." }));
    else {
      const list = el("ul", { class: "checks" });
      for (const c of found.constraints_checked) list.append(el("li", { class: "check " + (c.passed ? "pass" : "fail") }, el("b", { text: (c.passed ? "Passed: " : "Failed: ") + titleCase(c.name) }), el("span", { class: "muted", text: " " + c.detail })));
      box.append(el("p", { text: found.justification }), list,
        el("p", { class: "muted", text: `Confidence ${Math.round(found.confidence * 100)}% · source ${found.source}. ${s.alternatives.length ? "Compare the alternatives below." : ""}` }));
    }
    box.hidden = false;
  }
  async function confirmBooking(s) {
    const r = await api(`/api/plans/${state.planId}/confirm`, { method: "POST", body: { category: s.category } });
    if (r.ok && r.data.plan) { await refreshList(); await selectPlan(state.planId, null, "vendors"); toast(`${s.name} recorded as confirmed (version ${r.data.plan.version}).`); }
    else toast((r.data && r.data.violations && r.data.violations[0].detail) || (r.data && r.data.error) || "Could not record the confirmation.", 5000);
  }

  // "Confirmed by vendor" only when a vendor confirmation with evidence is on record; otherwise the user's word.
  async function markVendorConfirmations(root) {
    const r = await api(`/api/plans/${state.planId}/quotes`);
    if (!r.ok) return;
    for (const x of r.data) {
      const slot = root.querySelector(`.conf-badge[data-vendor="${x.vendor_id}"]`);
      if (slot && x.confirmed_by === "vendor") { clear(slot); slot.append(badge("Confirmed by vendor", "ok")); }
    }
  }

  // ------------------------------------------------------------- budget
  function panelBudget(root) {
    const p = state.plan, r = p.request;
    const cols = { venue: "var(--venue)", caterer: "var(--caterer)", photographer: "var(--photographer)" };
    const segs = p.selections.map((s) => ({ label: titleCase(s.category), value: s.cost, color: cols[s.category] }));
    const left = r.budget_total - p.total_cost - p.allocation.contingency;
    segs.push({ label: "Contingency reserve", value: p.allocation.contingency, color: "var(--organizer)" }, { label: left >= 0 ? "Unallocated" : "Over reserve", value: Math.abs(left), color: left >= 0 ? "var(--line)" : "var(--err)" });
    const R = 70, C = 2 * Math.PI * R, ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg"); svg.setAttribute("viewBox", "0 0 200 200"); svg.setAttribute("width", "220"); svg.setAttribute("height", "220"); svg.setAttribute("class", "donut"); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "Budget split");
    let offset = 0; const total = segs.reduce((a, s) => a + s.value, 0) || 1;
    for (const s of segs) {
      const len = (s.value / total) * C, c = document.createElementNS(ns, "circle");
      for (const [k, v] of Object.entries({ cx: 100, cy: 100, r: R, fill: "none", "stroke-width": 26, "stroke-dasharray": `${len} ${C - len}`, "stroke-dashoffset": -offset, transform: "rotate(-90 100 100)" })) c.setAttribute(k, v);
      c.style.stroke = s.color; svg.append(c); offset += len;
    }
    const t1 = document.createElementNS(ns, "text"); t1.setAttribute("x", 100); t1.setAttribute("y", 98); t1.setAttribute("text-anchor", "middle"); t1.setAttribute("font-size", "20"); t1.setAttribute("font-weight", "700"); t1.textContent = inr(p.total_cost).replace("INR ", "");
    const t2 = document.createElementNS(ns, "text"); t2.setAttribute("x", 100); t2.setAttribute("y", 118); t2.setAttribute("text-anchor", "middle"); t2.setAttribute("font-size", "11"); t2.textContent = "planned vendors"; svg.append(t1, t2);
    const legend = el("div", { class: "legend", style: { flexDirection: "column", gap: "8px" } }, ...segs.map((s) => el("span", {}, el("i", { style: { background: s.color } }), `${s.label}: ${inr(s.value)}`)));
    const rows = p.allocation.lines.map((l) => { const s = p.selections.find((x) => x.category === l.category); const ratio = s ? s.cost / (l.amount || 1) : 0;
      return el("tr", {}, el("td", { text: titleCase(l.category) }), el("td", { class: "num", text: inr(l.amount) }), el("td", { class: "num", text: s ? inr(s.cost) : "-" }), el("td", { style: { minWidth: "120px" } }, el("div", { class: "meter" }, el("i", { class: ratio > 1 ? "err" : "", style: { width: Math.min(100, ratio * 100) + "%" } })))); });
    rows.push(el("tr", {}, el("td", { text: "Contingency" }), el("td", { class: "num", text: inr(p.allocation.contingency) }), el("td", { class: "num", text: "held" }), el("td")));
    root.append(el("section", { class: "card" }, el("h2", { text: "Where the money goes" }), el("div", { class: "donut-wrap" }, svg, legend)),
      el("section", { class: "card" }, el("h2", { text: "Allocated vs planned" }),
        el("table", { class: "tbl" }, el("thead", {}, el("tr", {}, el("th", { text: "Category" }), el("th", { class: "num", text: "Allocated" }), el("th", { class: "num", text: "Planned" }), el("th", { text: "Used" }))), el("tbody", {}, ...rows),
          el("tfoot", {}, el("tr", {}, el("th", { text: "Total budget" }), el("th", { class: "num", text: inr(r.budget_total) }), el("th", { class: "num", text: inr(p.total_cost) }), el("th"))))));
    const ledger = el("section", { class: "card", id: "ledger" }, el("h2", { text: "Budget ledger" }));
    root.append(ledger);
    renderLedger(ledger);
    const quotes = el("section", { class: "card", id: "quotes" }, el("h2", { text: "Vendor quotes" }));
    root.append(quotes);
    renderQuotes(quotes);
    const scores = el("section", { class: "card", id: "scores" }, el("h2", { text: "Vendor scores" }));
    root.append(scores);
    renderScores(scores);
    const leads = el("section", { class: "card", id: "leads" }, el("h2", { text: "Saved leads" }));
    root.append(leads);
    renderLeads(leads);
  }

  // The F10 formula over recorded quotes, with weights and reasons. A component without data is left out, never guessed.
  async function renderScores(card) {
    const cats = [...new Set(state.plan.selections.map((x) => x.category))];
    const [dirRes, ...boards] = await Promise.all([api("/api/vendors/directory"), ...cats.map((c) => api(`/api/observations/score/${encodeURIComponent(c)}`))]);
    const dir = dirRes.ok ? dirRes.data : {};
    const who = (id) => { const d = dir[id]; return d ? `${d.name} (${d.label || d.origin})` : id; };  // a name and where it came from, never a raw id
    for (const n of [...card.children].slice(1)) n.remove();
    card.append(el("p", { class: "muted", text: "Computed from the quotes you recorded: price against the median of at least three current quotes, completeness of the checklist, and how well the quote is evidenced. Response time, terms and ratings are not scored: there is no data for them." }));
    let any = false;
    boards.forEach((r, i) => {
      if (!r.ok) return;
      const b = r.data;
      if (!b.scores.length && !b.unscored.length) return;
      any = true;
      card.append(el("h3", { text: titleCase(cats[i]) }));
      for (const s of b.scores) {
        card.append(el("div", { class: "alt score" }, el("b", { text: who(s.vendor_id) }), " ", el("span", { class: "tag eli", text: s.overall === null ? "n/a" : `${s.overall}/100` }),
          el("span", { class: "muted", text: ` ${Math.round(s.coverage * 100)}% of the formula had data (${s.status})` }),
          ...s.components.map((c) => el("div", { class: "muted", text: `• ${titleCase(c.name)} (${Math.round(c.weight * 100)}%): ${c.score === null ? "left out" : c.score}; ${c.detail}` })),
          ...s.notes.map((n) => el("div", { class: "bad", text: "! " + n }))));
      }
      for (const u of b.unscored) card.append(el("div", { class: "alt muted", text: `${who(u.vendor_id)}: not scored; ${u.reason}` }));
    });
    if (!any) card.append(el("p", { class: "muted", id: "scores-empty", text: "No quotes recorded yet. Record quotes above to compare vendors." }));
  }

  // ------------------------------------------------------------ community
  // Community events (ADR-046). Hard checks are computed; conditional items are the user's to mark; nothing here says a plan is safe or compliant.
  const ANSWER_LABELS = {
    public_event: "Is it open to the public?", third_party_premises: "Is the space owned by someone else (a venue, society or institution)?", amplified_sound: "Will you use loudspeakers or amplified music?",
    sells_food: "Will food be sold?", collects_money: "Will you collect or receive money from the public?", large_crowd: "Do you expect a large crowd?",
    temporary_structures: "Will you set up stages, tents or electrical rigging?", physical_activities: "Will there be physical or sports activities?", outdoor_area: "Is any part outdoors?",
  };
  const ACCESS_LABELS = { step_free_access: "Step-free access", accessible_toilet: "Accessible toilet", reserved_seating: "Reserved seating", hearing_support: "Hearing support", accessible_parking: "Accessible parking" };
  const PHASES = ["setup", "event", "teardown"];

  function panelCommunity(root) {
    const card = el("section", { class: "card", id: "community" }, el("h2", { text: "Community event" }));
    root.append(card);
    renderCommunity(card);
  }

  async function renderCommunity(card) {
    const base = `/api/plans/${encodeURIComponent(state.planId)}`;
    const r = await api(`${base}/community`);
    for (const n of [...card.children].slice(1)) n.remove();
    if (!r.ok) { card.append(el("p", { class: "muted", text: (r.data && r.data.error) || "Community details could not be loaded." })); return; }
    const d = r.data, again = () => renderCommunity(card);
    const msg = el("div", { class: "muted", id: "c-msg", role: "status" });
    const ver = verification(state.plan);
    card.append(el("div", { id: "c-verification", class: "notice " + (ver.state === "verified" ? "ok" : "warn"), text: ver.state === "verified" ? "Verified: every check passed and nothing required is unknown." : `${ver.label}. This is a provisional plan until it is resolved. ${ver.items.map((i) => i.repair).join(" ")}` }));
    card.append(
      el("p", { class: "muted", text: `${d.profile_label}. Profile v${d.profile_version}${d.profile_reviewed ? "" : ", not reviewed by any authority"}: its roles, budget shares and timing are adjustable defaults, not rules.` }),
      el("p", { id: "c-summary", class: "lead", text: d.summary }), el("p", { class: "muted", id: "c-summary-detail", text: d.summary_detail }));

    const checks = el("ul", { class: "checks", id: "c-checks" });
    for (const c of d.checks) checks.append(el("li", { class: "check " + (c.passed ? "pass" : "fail") }, el("b", { text: (c.passed ? "Passed: " : "Failed: ") + titleCase(c.name) }), el("span", { class: "muted", text: " " + c.detail })));
    card.append(el("h3", { text: "Checked from your details and the database" }), checks);
    if (d.unverified.length) {
      const un = el("ul", { class: "checks", id: "c-unverified" });
      for (const u of d.unverified) un.append(el("li", { class: "check warn" }, el("b", { text: "Could not be decided: " + titleCase(u.name) }), el("span", { class: "muted", text: ` ${u.detail}. To fix: ${u.repair}` })));
      card.append(el("h3", { text: "Not verified" }), un);
    }

    // ---- the editable details; every change is checked and saved as a new plan version, or refused with repairs
    const cd = JSON.parse(JSON.stringify(d.details || { subtype: d.subtype }));
    for (const k of ["funding", "slots", "shifts", "program", "volunteers", "access_needs"]) cd[k] = cd[k] || [];
    cd.answers = cd.answers || {};
    let formBox = el("div", { id: "c-form" });
    const drawForm = () => {
      const next = buildForm(cd, d, drawForm);
      formBox.replaceWith(next); formBox = next;
    };
    formBox = buildForm(cd, d, drawForm);
    const errors = el("div", { id: "c-errors", class: "bad", hidden: true });
    const save = el("button", { class: "btn primary", id: "c-save", type: "button", text: "Check and save these details", on: { click: async () => {
      clear(errors); errors.hidden = true;
      const body = cleanCommunity(cd);
      const res = await api(`${base}/community`, { method: "POST", body });
      if (res.ok && res.data.plan) { await refreshList(); await selectPlan(state.planId, null, "community"); toast(`Saved as version ${res.data.plan.version}.`); return; }
      const vs = (res.data && res.data.violations) || [];
      errors.hidden = false;
      if (vs.length) for (const v of vs) errors.append(el("p", {}, el("b", { text: titleCase(v.constraint) + ": " }), v.detail, el("br"), el("span", { class: "muted", text: "To fix: " + v.repair })));
      else errors.append(el("p", { text: (res.data && res.data.details && res.data.details[0] && res.data.details[0].message) || (res.data && res.data.error) || "Those details were refused." }));
    } } });
    card.append(el("h3", { text: "Your details" }), el("p", { class: "muted", text: "Fill in what you know. Saving checks it all; if something fails you see what and how to fix it, and nothing is changed." }), formBox, el("div", { class: "actions" }, save), errors);

    // ---- conditional requirements: only you can mark them
    card.append(el("h3", { text: "Things to check with others" }),
      el("p", { class: "muted", text: "Shown because of your answers. EventMate does not know the rules where you live and does not verify any of these: each says who to check with, and only you can mark it." }));
    if (!d.items.length) card.append(el("p", { class: "muted", id: "c-no-items", text: "No conditional items apply to your answers." }));
    for (const i of d.items) {
      const who = el("input", { class: "input", maxlength: "60", placeholder: "Your name", "aria-label": "Your name", value: state.senderName || "" });
      const note = el("input", { class: "input", maxlength: "200", placeholder: "Who told you, or why it does not apply", "aria-label": "Note", value: i.note || "" });
      const mark = (status) => async () => {
        if (!who.value.trim()) { msg.textContent = "Enter your name: the record says who confirmed it."; return; }
        const res = await api(`${base}/community/items/${i.item_id}`, { method: "POST", body: { status, by: who.value.trim(), on: state.today, note: note.value.trim() } });
        if (res.ok) again(); else msg.textContent = (res.data && res.data.error) || (res.data && res.data.details && res.data.details[0] && res.data.details[0].message) || "Could not record that.";
      };
      card.append(el("div", { class: "alt cond", "data-item": i.item_id },
        el("b", { text: i.title }), " ", el("span", { class: "tag " + (i.status === "not_verified" ? "rej" : "eli"), text: titleCase(i.status) }),
        el("div", { class: "muted", text: i.instruction + "." }),
        i.recorded_by ? el("div", { class: "muted", text: `Recorded by ${i.recorded_by} on ${i.recorded_on}${i.note ? ": " + i.note : ""}` }) : null,
        el("div", { class: "actions" }, who, note,
          el("button", { class: "btn small confirm-item", type: "button", text: "I confirmed this", on: { click: mark("confirmed_by_user") } }),
          el("button", { class: "btn small na-item", type: "button", text: "Does not apply", on: { click: mark("not_applicable_by_user") } }),
          i.status !== "not_verified" ? el("button", { class: "btn small", type: "button", text: "Reopen", on: { click: mark("not_verified") } }) : null)));
    }
    card.append(msg);

    // ---- what the venue said about access
    const venue = state.plan.selections.find((x) => x.category === "venue");
    if (venue) {
      const boxes = Object.keys(ACCESS_LABELS).map((k) => ({ k, box: el("input", { type: "checkbox", value: k, "aria-label": ACCESS_LABELS[k] }) }));
      const evidence = el("input", { class: "input", id: "a-evidence", maxlength: "200", placeholder: "Where the answer came from (email, call, date)", "aria-label": "Evidence" });
      const by = el("input", { class: "input", maxlength: "80", placeholder: "Your name", "aria-label": "Your name", value: state.senderName || "" });
      card.append(el("h3", { text: `What ${venue.name} told you about access` }),
        el("p", { class: "muted", text: "Ask the venue, then record its answer here. Tick only what the venue said it has; leave all unticked if it said none." }),
        el("div", { class: "actions", id: "a-boxes" }, ...boxes.map((b) => el("label", { class: "muted" }, b.box, " " + ACCESS_LABELS[b.k] + " "))),
        el("div", { class: "actions" }, evidence, by, el("button", { class: "btn small", id: "a-save", type: "button", text: "Record the venue's answer", on: { click: async () => {
          const res = await api(`/api/venues/${venue.vendor_id}/accessibility`, { method: "POST", body: { features: boxes.filter((b) => b.box.checked).map((b) => b.k), evidence_ref: evidence.value.trim(), entered_by: by.value.trim() } });
          if (res.ok) { await selectPlan(state.planId, null, "community"); } else msg.textContent = (res.data && res.data.details && res.data.details[0] && res.data.details[0].message) || (res.data && res.data.error) || "Could not record that.";
        } } })));
    }
  }

  const intOrNull = (v) => { const n = parseInt(v, 10); return Number.isFinite(n) ? n : null; };

  // The form edits the object in place, so what you typed survives adding or removing a row.
  function buildForm(cd, d, redraw) {
    const root = el("div", { id: "c-form" });
    const numInput = (obj, key, ph, id) => el("input", { class: "input", type: "number", min: "0", placeholder: ph, "aria-label": ph, id, value: obj[key] ?? "", on: { input: (ev) => { obj[key] = ev.target.value === "" ? null : parseInt(ev.target.value, 10); } } });
    const txt = (obj, key, ph, max = 60) => el("input", { class: "input", maxlength: String(max), placeholder: ph, "aria-label": ph, value: obj[key] ?? "", on: { input: (ev) => { obj[key] = ev.target.value; } } });
    const sel = (obj, key, opts, label) => el("select", { class: "input", "aria-label": label, on: { change: (ev) => { obj[key] = ev.target.value; } } }, ...opts.map(([v, t]) => el("option", { value: v, text: t, selected: obj[key] === v })));
    const rows = (title, list, make, blank, addText, id) => el("div", { id }, el("h4", { text: title }), ...list.map((row, i) => el("div", { class: "actions row" }, ...make(row, i), el("button", { class: "btn small", type: "button", text: "Remove", on: { click: () => { list.splice(i, 1); redraw(); } } }))),
      el("button", { class: "btn small", type: "button", text: addText, on: { click: () => { list.push(blank()); redraw(); } } }));

    root.append(
      el("h4", { text: "Attendance" }),
      el("div", { class: "actions" }, numInput(cd, "attendance_low", "Lowest expected attendance", "c-att-low"), numInput(cd, "attendance_high", "Highest expected attendance", "c-att-high")),
      el("p", { class: "muted", text: "The venue must hold the highest number. Leave blank to plan for the guest count alone." }),
      el("h4", { text: "Money" }),
      el("div", { class: "actions" }, numInput(cd, "own_funds_inr", "Your own funds (INR)", "c-own"),
        el("label", { class: "muted" }, el("input", { type: "checkbox", id: "c-count-committed", checked: !!cd.count_committed, on: { change: (ev) => { cd.count_committed = ev.target.checked; } } }), " Also count money people have committed (not yet received)")),
      el("p", { class: "muted", text: "The plan may spend your own funds plus money you record as received. Pledged money is never counted; committed money only if you tick the box, and it is then shown as such." }),
      rows("Funding sources", cd.funding, (f) => [txt(f, "label", "Who or what"), sel(f, "status", [["pledged", "Pledged (a hope)"], ["committed", "Committed (promised)"], ["received", "Received (in hand)"]], "Status"), numInput(f, "amount_inr", "Amount (INR)")],
        () => ({ label: "", status: "pledged", amount_inr: null }), "Add a funding source", "c-funding"),
      el("h4", { text: "Volunteers" }),
      el("textarea", { class: "input", id: "c-volunteers", rows: "3", placeholder: "One name per line", "aria-label": "Volunteers", on: { input: (ev) => { cd.volunteers = ev.target.value.split("\n").map((x) => x.trim()).filter(Boolean); } } }, cd.volunteers.join("\n")),
      el("div", { class: "actions" }, txt(cd, "setup_owner", "Who owns setup (a volunteer above)"), txt(cd, "cleanup_owner", "Who owns cleanup (a volunteer above)")),
      el("p", { class: "muted", text: "Roles the profile usually suggests (tap to add as a role you confirm you need): " }),
      el("div", { class: "chips", id: "c-suggested" }, ...d.suggested_roles.map((r) => el("button", { class: "chip", type: "button", title: r.note, text: r.role, on: { click: () => { cd.slots.push({ slot_id: "s" + (cd.slots.length + 1) + Math.floor(Math.random() * 90 + 10), role: r.role, phase: /setup/i.test(r.role) ? "setup" : /cleanup/i.test(r.role) ? "teardown" : "event", needed: 1, start_min: null, end_min: null }); redraw(); } } }))),
      rows("Roles you need (confirmed)", cd.slots, (s) => [txt(s, "role", "Role", 40), sel(s, "phase", PHASES.map((p) => [p, titleCase(p)]), "Phase"), numInput(s, "needed", "How many"), numInput(s, "start_min", "From minute (optional)"), numInput(s, "end_min", "To minute (optional)")],
        () => ({ slot_id: "s" + Math.floor(Math.random() * 9000 + 1000), role: "", phase: "event", needed: 1, start_min: null, end_min: null }), "Add a role", "c-slots"),
      rows("Who covers what", cd.shifts, (s) => [txt(s, "volunteer", "Volunteer"), txt(s, "role", "Role", 40), sel(s, "phase", PHASES.map((p) => [p, titleCase(p)]), "Phase"), numInput(s, "start_min", "From minute (optional)"), numInput(s, "end_min", "To minute (optional)")],
        () => ({ volunteer: "", role: "", phase: "event", start_min: null, end_min: null }), "Add a shift", "c-shifts"),
      rows("Programme", cd.program, (a) => [txt(a, "title", "Activity", 80), numInput(a, "minutes", "Minutes"), txt(a, "resource", "Stage or place (shared)", 20),
        sel(a, "after", [["", "No fixed order"], ...cd.program.filter((x) => x !== a && x.activity_id).map((x) => [x.activity_id, "After: " + (x.title || x.activity_id)])], "Comes after")],
        () => ({ activity_id: "p" + Math.floor(Math.random() * 90000 + 10000), title: "", minutes: 30, resource: "stage", after: "" }), "Add an activity", "c-program"),
      el("h4", { text: "Access needs of your guests" }),
      el("div", { class: "actions" }, ...Object.keys(ACCESS_LABELS).map((k) => el("label", { class: "muted" }, el("input", { type: "checkbox", checked: cd.access_needs.includes(k), "aria-label": ACCESS_LABELS[k], on: { change: (ev) => { const s = new Set(cd.access_needs); if (ev.target.checked) s.add(k); else s.delete(k); cd.access_needs = [...s]; } } }), " " + ACCESS_LABELS[k] + " "))),
      el("h4", { text: "Questions that decide what to check" }),
      ...Object.entries(ANSWER_LABELS).map(([k, q]) => el("div", { class: "actions" }, el("span", { text: q }), sel(cd.answers, k, [["unsure", "Not sure"], ["yes", "Yes"], ["no", "No"]], q))));
    return root;
  }

  // What the form holds, trimmed to what the server accepts: empty rows and blank numbers are dropped, not guessed.
  function cleanCommunity(cd) {
    const out = { subtype: cd.subtype, count_committed: !!cd.count_committed, answers: {} };
    for (const k of ["attendance_low", "attendance_high", "own_funds_inr"]) if (cd[k] !== null && cd[k] !== undefined && cd[k] !== "") out[k] = cd[k];
    out.funding = cd.funding.filter((f) => f.label && f.amount_inr > 0).map((f) => ({ label: f.label, status: f.status, amount_inr: f.amount_inr }));
    out.volunteers = cd.volunteers;
    for (const k of ["setup_owner", "cleanup_owner"]) if (cd[k]) out[k] = cd[k];
    out.slots = cd.slots.filter((s) => s.role).map((s) => { const o = { slot_id: s.slot_id, role: s.role, phase: s.phase, needed: s.needed || 1 }; if (s.start_min) o.start_min = s.start_min; if (s.end_min) o.end_min = s.end_min; return o; });
    out.shifts = cd.shifts.filter((s) => s.volunteer && s.role).map((s) => { const o = { volunteer: s.volunteer, role: s.role, phase: s.phase }; if (s.start_min) o.start_min = s.start_min; if (s.end_min) o.end_min = s.end_min; return o; });
    out.program = cd.program.filter((a) => a.title).map((a) => { const o = { activity_id: a.activity_id, title: a.title, minutes: a.minutes || 30, resource: (a.resource || "stage").toLowerCase().replace(/[^a-z0-9_]/g, "_").slice(0, 20) }; if (a.after) o.after = a.after; return o; });
    out.access_needs = cd.access_needs;
    for (const [k, v] of Object.entries(cd.answers)) if (v) out.answers[k] = v;
    return out;
  }

  // ------------------------------------------------------------ ideas
  // Personalized suggestions (ADR-045): never verified offers, never a price. Pinning one adds a note or checklist item beside the plan; the plan itself is untouched.
  function panelIdeas(root) {
    const card = el("section", { class: "card", id: "ideas" }, el("h2", { text: "Ideas" }));
    root.append(card);
    renderIdeas(card);
  }

  async function renderIdeas(card) {
    const base = `/api/plans/${encodeURIComponent(state.planId)}`;
    const r = await api(`${base}/ideas`);
    for (const n of [...card.children].slice(1)) n.remove();
    if (!r.ok) { card.append(el("p", { class: "muted", text: "Ideas could not be loaded." })); return; }
    const d = r.data, msg = el("div", { class: "muted", id: "ideas-msg", role: "status" });
    const prefs = el("input", { id: "ideas-prefs", class: "input", maxlength: "300", placeholder: "Preferences, comma separated (optional, up to 5): rustic, kid friendly", "aria-label": "Preferences", value: state.ideaPrefs || "", on: { input: (ev) => { state.ideaPrefs = ev.target.value; } } });
    const go = el("button", { class: "btn primary", id: "ideas-go", type: "button", text: d.ideas.length ? "Suggest again" : "Suggest ideas", on: { click: async () => {
      const list = (state.ideaPrefs || "").split(",").map((x) => x.trim()).filter(Boolean);
      const res = await api(`${base}/ideas`, { method: "POST", body: { preferences: list } });
      if (res.ok) renderIdeas(card); else msg.textContent = (res.data && res.data.details && res.data.details[0] && res.data.details[0].message) || (res.data && res.data.error) || "Could not suggest ideas.";
    } } });
    card.append(
      el("p", { class: "muted", id: "ideas-notice", text: d.notice + ". " + "They come from your plan's facts, carry no prices and never change the plan. Pin one to keep it as a note or checklist item." }),
      el("div", { class: "actions" }, prefs, go), msg);
    if (d.source) card.append(el("p", { class: "muted", text: `Made ${d.source.startsWith("llm:") ? "with a model (" + d.source.slice(4) + "), checked by rules" : "from built-in templates"} using: ${d.facts_used.map(titleCase).join(", ")}.` }));
    for (const i of d.ideas) {
      card.append(el("div", { class: "alt idea", "data-idea": i.idea_id },
        el("span", { class: "tag", text: titleCase(i.kind) }), " ", el("b", { text: i.title }),
        el("div", { class: "muted", text: i.why }), i.est_cost_note ? el("div", { class: "muted", text: "Cost: " + i.est_cost_note }) : null,
        i.review_note ? el("div", { class: "bad idea-review", text: i.review_note }) : null,
        el("div", { class: "actions" },
          d.pinned.includes(i.idea_id) ? el("span", { class: "tag eli", text: "pinned" }) : el("button", { class: "btn small pin", type: "button", text: "Pin", on: { click: async () => { const res = await api(`${base}/ideas/${i.idea_id}/pin`, { method: "POST", body: {} }); if (res.ok) renderIdeas(card); else msg.textContent = (res.data && res.data.error) || "Could not pin."; } } }),
          el("button", { class: "btn small dismiss", type: "button", text: "Dismiss", on: { click: async () => { const res = await api(`${base}/ideas/${i.idea_id}/dismiss`, { method: "POST", body: {} }); if (res.ok) renderIdeas(card); else msg.textContent = (res.data && res.data.error) || "Could not dismiss."; } } }))));
    }
    if (d.dismissed.length) card.append(el("p", { class: "muted", text: `${d.dismissed.length} dismissed.` }));
    if (!d.ideas.length && !d.source) card.append(el("p", { class: "muted", id: "ideas-empty", text: "No ideas yet. Press Suggest ideas." }));
    const notes = el("div", { id: "notes" }, el("h3", { text: "Notes and checklist" }));
    if (!d.notes.length) notes.append(el("p", { class: "muted", text: "Nothing pinned yet." }));
    for (const n of d.notes) {
      notes.append(el("label", { class: "note", "data-note": n.note_id, style: { display: "block" } },
        el("input", { type: "checkbox", checked: n.done, "aria-label": n.text, on: { change: async (ev) => { const res = await api(`${base}/notes/${n.note_id}/done`, { method: "POST", body: { done: ev.target.checked } }); if (res.ok) renderIdeas(card); } } }),
        " ", el("span", { text: n.text }), " ", el("span", { class: "muted", text: n.kind === "checklist_item" ? "(checklist)" : "(note)" })));
    }
    card.append(notes);
  }

  // ------------------------------------------------------------ vendor chats
  // The agent writes to vendors and reads their replies. Nothing leaves without your approval of that exact text; replies are shown as the vendor's own words.
  async function refreshAttention() {
    if (!state.planId) return;
    const r = await api(`/api/plans/${encodeURIComponent(state.planId)}/inbox`);
    const jr = await api(`/api/plans/${encodeURIComponent(state.planId)}/jobs`);
    const jobItems = jr.ok ? jr.data.filter((j) => !["done", "expired", "cancelled"].includes(j.status)).reduce((a, j) => a + j.items.filter((i) => !i.resolved).length, 0) : 0;
    const n = (r.ok ? r.data.unread_total + r.data.approvals_total : 0) + jobItems;
    if (n !== state.attention) { state.attention = n; renderTabs(); }
  }

  function panelTalk(root) {
    const card = el("section", { class: "card", id: "talk" }, el("h2", { text: "Vendor chats" }));
    root.append(card);
    renderTalk(card);
    state.talkTimer = setInterval(() => { if (!["INPUT", "TEXTAREA", "SELECT"].includes((document.activeElement || {}).tagName)) renderTalk(card); }, 20000);
  }

  const STATUS_TAG = { blocked: ["blocked: do not contact", "rej"], prepared: ["waiting for you", "rej"], approved: ["approved, sending soon", "eli"], sending: ["sending", "eli"], sent: ["sent", "eli"], failed: ["not sent", "rej"], outcome_unknown: ["unknown: check the chat", "rej"], denied: ["you declined", "rej"], expired: ["expired", "rej"], marked_sent: ["you sent it", "eli"] };
  const safeLink = (u) => /^(mailto:|https:\/\/wa\.me\/)/.test(u);

  async function renderTalk(card) {
    const pe = encodeURIComponent(state.planId);
    const [box, contacts, auto, jobsRes, digestsRes, notifyRes, shortRes, labelRes, supRes, unmRes] = await Promise.all([api(`/api/plans/${pe}/inbox`), api("/api/contacts"), api(`/api/plans/${pe}/autoreply`), api(`/api/plans/${pe}/jobs`), api(`/api/plans/${pe}/digests`), api("/api/notify"), api(`/api/plans/${pe}/shortlist`), api("/api/vendors/labels"), api("/api/suppressions"), api("/api/unmatched")]);
    for (const n of [...card.children].slice(1)) n.remove();
    if (!box.ok) { card.append(el("p", { class: "muted", text: "Vendor chats could not be loaded." })); return; }
    const inbox = box.data, saved = contacts.ok ? contacts.data : [];
    state.labels = labelRes.ok ? labelRes.data : {};
    const msg = el("div", { class: "muted", id: "o-msg", role: "status" });
    const again = () => { renderTalk(card); refreshAttention(); };
    const planBase = `/api/plans/${encodeURIComponent(state.planId)}`;
    const fail = (res, fallback) => { msg.textContent = (res.data && res.data.details && res.data.details[0] && res.data.details[0].message) || (res.data && res.data.error) || fallback; };

    const canSend = Object.fromEntries(inbox.channels.map((c) => [c.channel, c.can_send]));
    const strip = el("div", { class: "badges", id: "o-channels" }, ...inbox.channels.map((c) => badge(`${titleCase(c.channel)}: ${c.can_send ? "ready" : c.detail}`, c.can_send ? "ok" : "warn")));
    const name = el("input", { id: "o-by", class: "input", maxlength: "80", placeholder: "Your name (signed on messages)", "aria-label": "Your name", value: state.senderName || "", on: { input: (ev) => { state.senderName = ev.target.value; } } });
    const needName = () => { if (!(state.senderName || "").trim()) { msg.textContent = "Enter your name first: it is signed at the end of each message."; return false; } return true; };

    const all = el("button", { class: "btn primary", id: "o-all", type: "button", text: "Draft requests for all vendors with a saved contact", on: { click: async () => {
      if (!needName()) return;
      const res = await api(`${planBase}/outreach/request-all`, { method: "POST", body: { sender_name: state.senderName.trim() } });
      if (res.ok) { toast(`${res.data.drafts.length} draft${res.data.drafts.length === 1 ? "" : "s"} ready for your approval.` + (res.data.skipped.length ? " " + res.data.skipped.join("; ") : ""), 6000); again(); } else fail(res, "Could not draft the requests.");
    } } });
    const poll = el("button", { class: "btn", id: "o-poll", type: "button", text: "Check for replies now", on: { click: async () => {
      const res = await api("/api/inbox/poll", { method: "POST", body: {} });
      if (!res.ok) { fail(res, "Could not check."); return; }
      await api("/api/jobs/tick", { method: "POST", body: {} });  // job items (prices to confirm, questions) update from the replies just read
      const r = res.data;
      toast(`${r.new_messages} new repl${r.new_messages === 1 ? "y" : "ies"}, ${r.sent} sent.` + (r.errors.length ? " " + r.errors.join("; ") : "") + (r.waiting.length ? " Waiting: " + r.waiting.join("; ") : ""), 6000);
      again();
    } } });

    const vendorSel = el("select", { id: "c-vendor", class: "input", "aria-label": "Vendor" }, ...state.plan.selections.map((x) => el("option", { value: x.vendor_id, text: `${titleCase(x.category)}: ${x.name}` })));
    const cEmail = el("input", { id: "c-email", class: "input", maxlength: "254", placeholder: "Email (optional)", "aria-label": "Vendor email" });
    const cWa = el("input", { id: "c-wa", class: "input", maxlength: "30", placeholder: "WhatsApp number +91… (optional)", "aria-label": "Vendor WhatsApp number" });
    const cPref = el("select", { id: "c-pref", class: "input", "aria-label": "Preferred channel" }, el("option", { value: "whatsapp", text: "Prefer WhatsApp" }), el("option", { value: "email", text: "Prefer email" }));
    const saveContact = el("button", { class: "btn", id: "c-save", type: "button", text: "Save contact", on: { click: async () => {
      const res = await api("/api/contacts", { method: "POST", body: { vendor_id: vendorSel.value, email: cEmail.value.trim() || null, whatsapp: cWa.value.trim() || null, preferred: cPref.value } });
      if (res.ok) again(); else fail(res, "That contact was refused.");
    } } });

    // Standing permission (ADR-043): fixed follow-up questions only, bounded, revocable. The stop button is always visible.
    const ar = auto.ok ? auto.data : null;
    let autoBox = null;
    if (ar) {
      const m = ar.mandate;
      const hours = el("select", { id: "a-hours", class: "input", "aria-label": "How long" }, ...[[24, "for 24 hours"], [72, "for 3 days"], [168, "for 7 days"]].map(([v, t]) => el("option", { value: v, text: t })));
      const perThread = el("select", { id: "a-per", class: "input", "aria-label": "Messages per vendor" }, ...[1, 2, 3, 5].map((n) => el("option", { value: n, text: `up to ${n} per vendor`, selected: n === 3 })));
      const turnOn = el("button", { class: "btn primary", id: "a-on", type: "button", text: "Allow automatic follow-up questions", on: { click: async () => {
        if (!needName()) return;
        const res = await api(`${planBase}/autoreply`, { method: "POST", body: { sender_name: state.senderName.trim(), hours: parseInt(hours.value, 10), max_per_thread: parseInt(perThread.value, 10), max_total: 10 } });
        if (res.ok) again(); else fail(res, "Could not turn that on.");
      } } });
      const turnOff = el("button", { class: "btn small", id: "a-off", type: "button", text: "Turn off for this plan", on: { click: async () => { const res = await api(`${planBase}/autoreply/revoke`, { method: "POST", body: {} }); if (res.ok) again(); else fail(res, "Could not turn that off."); } } });
      const kill = el("button", { class: "btn danger", id: "a-kill", type: "button", text: "STOP all automatic messages now", on: { click: async () => { const res = await api("/api/autoreply/kill", { method: "POST", body: {} }); if (res.ok) { toast("Every automatic message is stopped, including queued ones.", 5000); again(); } else fail(res, "Could not stop them."); } } });
      const resume = el("button", { class: "btn small", id: "a-resume", type: "button", text: "Lift the stop", on: { click: async () => { const res = await api("/api/autoreply/resume", { method: "POST", body: {} }); if (res.ok) again(); else fail(res, "Could not lift it."); } } });
      autoBox = el("section", { class: "alt", id: "autoreply" }, el("b", { text: "Automatic follow-up questions" }), " ",
        ar.killed ? badge("stopped by the kill switch", "err") : ar.active ? badge(`on until ${new Date(m.expires_at).toLocaleString()} · ${ar.sent_total} of ${m.max_total} used`, "ok") : badge("off", ""),
        el("p", { class: "muted", text: "When on, the agent may answer a vendor's reply with one fixed question about something still missing (price, what is included, your date, how long the quote is valid) without asking you each time. It never offers or accepts a price, never mentions your budget, never answers a question they asked you, and hands the chat to you if they refuse, ask you something, send a file or ask to stop. It sends only between 09:00 and 20:00 IST and every message says it was automatic." }),
        el("div", { class: "actions" }, ar.active ? turnOff : [hours, perThread, turnOn], ar.killed ? resume : kill));
    }

    card.append(
      el("p", { class: "muted", text: "EventMate writes to your vendors and reads what they answer. Every message waits for your approval of its exact text, goes out through your own email or WhatsApp gateway, and never contains your budget. Replies are shown as the vendor's own words; nothing becomes a quote until you record it." }),
      strip,
      inbox.channels.some((c) => !c.can_send) ? el("p", { class: "muted", text: "A channel that is not ready can still be used by hand: open the link on a draft, send it yourself, then press I sent it. WhatsApp through an unofficial gateway can get the number restricted by WhatsApp, so use a number you can afford to lose." }) : null,
      autoBox,
      el("div", { class: "actions" }, name, all, poll),
      el("div", { class: "actions" }, vendorSel, cEmail, cWa, cPref, saveContact),
      saved.length ? el("p", { class: "muted", id: "c-list", text: "Saved contacts: " + saved.map((c) => `${c.vendor_id} (${[c.email && "email", c.whatsapp && "WhatsApp"].filter(Boolean).join(" + ")})`).join(", ") }) : null,
      msg);

    card.append(updatesView(digestsRes.ok ? digestsRes.data : [], notifyRes.ok ? notifyRes.data : null, { planBase, again, fail }));
    card.append(findVendorsView(shortRes.ok ? shortRes.data : [], labelRes.ok ? labelRes.data : {}, { planBase, again, fail, needName }));
    card.append(jobsView(jobsRes.ok ? jobsRes.data : [], saved, { planBase, again, fail, needName }));
    card.append(suppressionView(supRes.ok ? supRes.data : [], unmRes.ok ? unmRes.data : [], { again, fail }));
    if (!inbox.threads.length) card.append(el("p", { class: "muted", id: "o-empty", text: "No conversations yet. Save a vendor contact, then draft the requests." }));
    for (const t of inbox.threads) card.append(threadView(t, { planBase, canSend, again, fail, needName }));
    // writing to one vendor by typing a contact once, without saving it
    const oneCat = el("select", { id: "o-category", class: "input", "aria-label": "Vendor" }, ...state.plan.selections.map((x) => el("option", { value: x.category, text: `${titleCase(x.category)}: ${x.name}` })));
    const oneCh = el("select", { id: "o-channel", class: "input", "aria-label": "Channel" }, el("option", { value: "whatsapp", text: "WhatsApp" }), el("option", { value: "email", text: "Email" }));
    const oneTo = el("input", { id: "o-to", class: "input", maxlength: "254", placeholder: "WhatsApp number (+91…) or email", "aria-label": "Vendor contact" });
    const one = el("button", { class: "btn", id: "o-make", type: "button", text: "Draft one request", on: { click: async () => {
      if (!oneTo.value.trim() || !needName()) return;
      const res = await api(`${planBase}/outreach`, { method: "POST", body: { category: oneCat.value, channel: oneCh.value, recipient: oneTo.value.trim(), sender_name: state.senderName.trim() } });
      if (res.ok) again(); else fail(res, "That request was refused.");
    } } });
    card.append(el("h3", { text: "Ask one vendor without saving a contact" }), el("div", { class: "actions" }, oneCat, oneCh, oneTo, one));
  }

  // ------------------------------------------------------------ find vendors
  // Discovered businesses (ADR-049): found by a search, public contact read from their own pages, each address confirmed by you before anything is written to it.
  const ORIGIN_LABEL = { discovered: "discovered", test: "TEST VENDOR" };
  const SOURCE_LABEL = { osm: "OpenStreetMap", gplaces: "Google Places", test: "your own test address" };
  const BLOCK_NOTE = "Check the anti-spam and privacy rules that apply to you before contacting businesses. EventMate does not verify them.";

  function findVendorsView(items, labels, ctx) {
    const { planBase, again, fail, needName } = ctx;
    const box = el("section", { class: "alt", id: "find-vendors" }, el("b", { text: "Find vendors" }),
      el("p", { class: "muted", text: "Find businesses, read the public contact details they publish on their own pages, review each address, then start a quote job. Nothing is contacted until you approve the exact message. Coverage depends on what a source publishes and varies by city and category." }),
      el("p", { class: "muted", id: "fv-note", text: BLOCK_NOTE }));
    const msg = el("div", { class: "muted", id: "fv-msg", role: "status" });
    const cat = el("select", { id: "fv-cat", class: "input", "aria-label": "Category" }, ...["venue", "caterer", "photographer"].map((c) => el("option", { value: c, text: titleCase(c) })));
    const src = el("select", { id: "fv-src", class: "input", "aria-label": "Source" }, el("option", { value: "", text: "Best source" }), el("option", { value: "osm", text: "OpenStreetMap (venues)" }), el("option", { value: "places", text: "Google Places (needs a key)" }));
    box.append(el("div", { class: "actions" }, cat, src, el("button", { class: "btn primary", id: "fv-search", type: "button", text: "Search", on: { click: async () => {
      const body = { category: cat.value }; if (src.value) body.source = src.value;
      const r = await api(`${planBase}/shortlist/search`, { method: "POST", body });
      if (r.ok) { toast(`${r.data.added} new, ${r.data.total} shortlisted from ${r.data.source}.` + (r.data.warnings.length ? " " + r.data.warnings.join("; ") : ""), 5000); again(); }
      else msg.textContent = (r.data && r.data.status === "not_supported" ? "Not supported by this source: " : "") + ((r.data && r.data.error) || "The search failed.");
    } } })), msg);

    const picks = [];
    for (const it of items) {
      const b = it.business, test = b.is_test;
      const label = labels[b.vendor_id];
      const row = el("div", { class: "alt biz", "data-vendor": b.vendor_id },
        el("b", { text: b.name }), " ", el("span", { class: "tag " + (test ? "rej" : "eli"), text: test ? label || "TEST VENDOR" : "Discovered" }), " ",
        el("span", { class: "muted", text: `${titleCase(it.category)} · from ${SOURCE_LABEL[b.source]}${b.rating ? ` · rated ${b.rating}` : ""}` }),
        b.source_url ? el("div", { class: "muted" }, "Source: ", el("a", { href: b.source_url, target: "_blank", rel: "noopener noreferrer nofollow", text: b.source_url })) : null,
        b.website ? el("div", { class: "muted" }, `Website${b.website_source === "user_input" ? " (typed by you)" : ""}: `, el("a", { href: b.website, target: "_blank", rel: "noopener noreferrer nofollow", text: b.website })) : null);
      if (!test && !b.website) {
        const site = el("input", { class: "input", maxlength: "2048", placeholder: "Its website (the listing had none)", "aria-label": "Website" });
        row.append(el("div", { class: "actions" }, site, el("button", { class: "btn small save-site", type: "button", text: "Save website", on: { click: async () => { const r = await api(`${planBase}/shortlist/${b.vendor_id}/website`, { method: "POST", body: { url: site.value.trim() } }); if (r.ok) again(); else fail(r, "That website was refused."); } } })));
      }
      if (!test && b.website && !it.contact) row.append(el("button", { class: "btn small find-contacts", type: "button", text: "Find public contact", on: { click: async () => {
        const r = await api(`${planBase}/shortlist/${b.vendor_id}/find-contacts`, { method: "POST", body: {} });
        if (r.ok) { if (r.data.notes.length) toast(r.data.notes.join(" "), 6000); again(); } else fail(r, "Could not read its pages.");
      } } }));
      for (const c of it.candidates.filter((x) => x.status === "found")) {
        const edit = el("input", { class: "input cand-value", maxlength: "254", value: c.value, "aria-label": "Address to confirm" });
        const who = el("input", { class: "input", maxlength: "60", placeholder: "Your name", "aria-label": "Your name", value: state.senderName || "" });
        const ackHolder = el("span", {});
        let ackBox = null;
        row.append(el("div", { class: "alt cand", "data-candidate": c.candidate_id },
          el("div", { class: "muted" }, `Found ${c.source_kind === "page_mailto" ? "as a mail link" : c.source_kind === "page_text" ? "in the page text" : "in the listing"} on `, el("a", { href: c.source_url, target: "_blank", rel: "noopener noreferrer nofollow", text: c.source_url })),
          ...c.flags.map((f) => el("div", { class: "bad", text: "! " + f })),
          el("div", { class: "actions" }, edit, who,
            el("button", { class: "btn primary small confirm-cand", type: "button", text: "Confirm this address", on: { click: async () => {
              if (!who.value.trim()) { msg.textContent = "Enter your name: the record says who confirmed it."; return; }
              const body = { by: who.value.trim() }; if (edit.value.trim() !== c.value) body.edited_value = edit.value.trim();
              if (ackBox && ackBox.checked) body.acknowledge_bounce = true;
              const r = await api(`${planBase}/shortlist/${b.vendor_id}/contacts/${c.candidate_id}/confirm`, { method: "POST", body });
              if (r.ok) { again(); return; }
              if (r.data && r.data.needs_acknowledgement && !ackBox) {  // this address bounced before: show why, and ask for a second, informed click
                ackBox = el("input", { type: "checkbox", class: "ack-bounce", "aria-label": "I have read why it bounced" }); ackHolder.append(ackBox, " I have read why it bounced");
              }
              fail(r, "That address was refused.");
            } } }),
            ackHolder,
            el("button", { class: "btn small reject-cand", type: "button", text: "Not theirs", on: { click: async () => { const r = await api(`${planBase}/shortlist/${b.vendor_id}/contacts/${c.candidate_id}/reject`, { method: "POST", body: {} }); if (r.ok) again(); else fail(r, "Could not reject it."); } } }))));
      }
      if (it.contact) {
        const c = it.contact;
        row.append(el("div", { class: "msg out", "data-confirmed": b.vendor_id },
          el("div", {}, el("b", { text: "Confirmed address: " }), c.email || c.whatsapp, c.edited_by_user ? " (edited by you)" : ""),
          el("div", { class: "muted", text: `Confirmed by ${c.confirmed_by}${c.source_url ? ", read from " + c.source_url : test ? ", an address you own" : ""}` }),
          it.blocked ? el("div", { class: "bad", text: "Blocked: " + it.blocked + ". Only you can lift it." }) : null));
        if (!it.blocked) { const tick = el("input", { type: "checkbox", value: b.vendor_id, "aria-label": "Ask " + b.name }); picks.push({ id: b.vendor_id, cat: it.category, box: tick }); row.append(el("label", { class: "muted" }, tick, " Include in a quote job")); }
      }
      box.append(row);
    }
    if (!items.length) box.append(el("p", { class: "muted", id: "fv-empty", text: "No businesses shortlisted yet. Search above." }));
    if (picks.length) {
      const goalCat = () => { const cats = [...new Set(picks.filter((p) => p.box.checked).map((p) => p.cat))]; return cats.length === 1 ? cats[0] : null; };
      box.append(el("div", { class: "actions" }, el("button", { class: "btn primary", id: "fv-start", type: "button", text: "Draft quote requests for the ticked businesses", on: { click: async () => {
        const chosen = picks.filter((p) => p.box.checked); const category = goalCat();
        if (!needName()) return;
        if (!chosen.length) { msg.textContent = "Tick the businesses to ask."; return; }
        if (!category) { msg.textContent = "Tick businesses of one category at a time."; return; }
        const r = await api(`${planBase}/jobs`, { method: "POST", body: { category, vendor_ids: chosen.map((p) => p.id), goal: `${chosen.length} comparable ${category} quote${chosen.length === 1 ? "" : "s"}`, sender_name: state.senderName.trim(), deadline_days: 7, min_quotes: chosen.length } });
        if (r.ok) again(); else fail(r, "Could not start the job.");
      } } }), el("span", { class: "muted", text: "Only addresses you confirmed can be asked. You still approve the exact text before anything is sent." })));
    }
    // a vendor whose address is yours, for testing: needs your statement that you own it, and is labelled TEST VENDOR everywhere
    const tn = el("input", { class: "input", id: "tv-name", maxlength: "120", placeholder: "Test vendor name", "aria-label": "Test vendor name" });
    const te = el("input", { class: "input", id: "tv-email", maxlength: "254", placeholder: "An address you own", "aria-label": "Test address" });
    const own = el("input", { type: "checkbox", id: "tv-own", "aria-label": "I own this address" });
    const tc = el("select", { id: "tv-cat", class: "input", "aria-label": "Test vendor category" }, ...["caterer", "venue", "photographer"].map((c) => el("option", { value: c, text: titleCase(c) })));
    box.append(el("details", { class: "fold" }, el("summary", { text: "Add a test vendor (an address you own)" }),
      el("div", { class: "actions" }, tn, te, tc, el("label", { class: "muted" }, own, " I own this address"),
        el("button", { class: "btn small", id: "tv-add", type: "button", text: "Add test vendor", on: { click: async () => {
          if (!needName()) return;
          const r = await api(`${planBase}/shortlist/test-vendor`, { method: "POST", body: { name: tn.value.trim(), category: tc.value, email: te.value.trim(), owns_address: own.checked, by: state.senderName.trim() } });
          if (r.ok) again(); else fail(r, "That test vendor was refused.");
        } } }))));
    return box;
  }

  // The do-not-contact list and mail that is not tied to anything we sent. Only you add to or lift from the list by hand; a matched stop request adds itself.
  function suppressionView(list, unmatched, ctx) {
    const { again, fail } = ctx;
    const box = el("section", { class: "alt", id: "dnc" }, el("b", { text: "Do not contact" }),
      el("p", { class: "muted", text: "One entry covers an address on every plan and job, and stops drafts that were already queued. A stop request in a reply to a message we sent, or a permanent delivery failure that names a message we sent, adds an entry. Neither is authenticated: mail headers can be forged, so read anything that surprises you. A forgery can only stop us writing to an address we already wrote to, and only you can lift an entry." }));
    for (const s of list.filter((x) => x.active)) {
      const why = el("input", { class: "input", maxlength: "200", placeholder: "Why it is lifted", "aria-label": "Reason" });
      const by = el("input", { class: "input", maxlength: "80", placeholder: "Your name", "aria-label": "Your name", value: state.senderName || "" });
      const bounced = s.reason === "bad_address";
      const ack = bounced ? el("input", { type: "checkbox", class: "ack-reason", "aria-label": "I have read why it failed" }) : null;
      box.append(el("div", { class: "alt sup", "data-sup": s.suppression_id }, el("b", { text: s.value }), " ", el("span", { class: "tag rej", text: s.reason.replace("_", " ") }), el("div", { class: "muted", text: `${s.note}. Added by ${s.created_by}.` }),
        s.detail ? el("div", { class: "bad sup-detail", text: `Why it failed: ${s.detail} (the remote mail server's own words, not verified)` }) : null,
        el("div", { class: "actions" }, why, by, ack ? el("label", { class: "muted" }, ack, " I have read why it failed") : null, el("button", { class: "btn small lift", type: "button", text: "Lift", on: { click: async () => {
          const r = await api(`/api/suppressions/${s.suppression_id}/lift`, { method: "POST", body: { by: by.value.trim(), reason: why.value.trim(), acknowledge_reason: !!(ack && ack.checked) } }); if (r.ok) again(); else fail(r, "Say who is lifting it and why.");
        } } }))));
    }
    if (!list.some((x) => x.active)) box.append(el("p", { class: "muted", text: "No addresses are on the list." }));
    if (unmatched.length) {
      box.append(el("h4", { text: "Messages to review" }), el("p", { class: "muted", text: "These look like a stop request or a delivery failure but are not tied to anything we sent, so they changed nothing." }));
      for (const m of unmatched) {
        const by = el("input", { class: "input", maxlength: "80", placeholder: "Your name", "aria-label": "Your name", value: state.senderName || "" });
        box.append(el("div", { class: "alt unm", "data-unmatched": m.message_id }, el("b", { text: `${m.kind === "bounce" ? "Delivery failure" : "Stop-like message"} from ${m.sender}` }), el("pre", { class: "muted", style: { whiteSpace: "pre-wrap" }, text: m.body }),
          el("div", { class: "actions" }, by, m.kind === "stop_like" ? el("button", { class: "btn small suppress-unm", type: "button", text: "Do not contact this sender", on: { click: async () => { const r = await api(`/api/unmatched/${m.message_id}/suppress`, { method: "POST", body: { by: by.value.trim() } }); if (r.ok) again(); else fail(r, "Enter your name."); } } }) : null,
            el("button", { class: "btn small dismiss-unm", type: "button", text: "Dismiss", on: { click: async () => { const r = await api(`/api/unmatched/${m.message_id}/dismiss`, { method: "POST", body: {} }); if (r.ok) again(); } } }))));
      }
    }
    return box;
  }

  // Job updates (ADR-047): always kept here. An email copy to you is optional, off by default, and needs you to confirm the exact address and what it may contain.
  function updatesView(digests, notify, ctx) {
    const { planBase, again, fail } = ctx;
    const box = el("section", { class: "alt", id: "updates" }, el("b", { text: "Updates" }));
    if (!digests.length) box.append(el("p", { class: "muted", id: "updates-empty", text: "Nothing yet. Updates from your vendor jobs appear here when something changes or needs you." }));
    for (const d of digests.slice(0, 8)) {
      box.append(el("div", { class: "msg in digest", "data-digest": d.digest_id, "data-unread": String(!d.read) },
        el("div", { class: "muted", text: new Date(d.created_at).toLocaleString() }), d.read ? null : el("span", { class: "tag eli", text: "new" }),
        el("pre", { style: { whiteSpace: "pre-wrap" }, text: d.text }),
        d.read ? null : el("button", { class: "btn small", type: "button", text: "Mark read", on: { click: async () => { await api(`${planBase}/digests/${d.digest_id}/read`, { method: "POST", body: {} }); again(); } } })));
    }
    if (!notify) return box;
    const msg = el("div", { class: "muted", id: "notify-msg", role: "status" });
    const sec = el("div", { id: "notify" }, el("h4", { text: "Email me a copy" }));
    if (notify.state === "on") {
      sec.append(el("p", { class: "muted", id: "notify-state", text: `On: updates go to ${notify.destination} and no one else. They include job status and counts${notify.include_vendor_names ? ", vendor names" : ""}${notify.include_amounts ? ", confirmed amounts" : ""}${notify.include_raw_text ? ", the vendors' wording" : ""}; never your budget. Sent ${notify.sent_last_day} of at most ${notify.per_day} in the last day.` }),
        el("button", { class: "btn small", id: "notify-off", type: "button", text: "Turn off", on: { click: async () => { const r = await api("/api/notify/disable", { method: "POST", body: {} }); if (r.ok) again(); else fail(r, "Could not turn it off."); } } }));
    } else {
      const to = el("input", { class: "input", id: "notify-to", maxlength: "254", placeholder: "Your own email address", "aria-label": "Your email address", value: notify.destination || "" });
      const opts = [["include_vendor_names", "vendor names"], ["include_amounts", "confirmed amounts"], ["include_raw_text", "the vendors' own wording"]].map(([k, t]) => ({ k, t, box: el("input", { type: "checkbox", "aria-label": t, checked: !!notify[k] }) }));
      const review = el("button", { class: "btn small", id: "notify-review", type: "button", text: "Review and enable", on: { click: async () => {
        const body = { destination: to.value.trim() }; for (const o of opts) body[o.k] = o.box.checked;
        const r = await api("/api/notify/propose", { method: "POST", body });
        if (!r.ok) { msg.textContent = (r.data && r.data.error) || (r.data && r.data.details && r.data.details[0] && r.data.details[0].message) || "That address was refused."; return; }
        clear(confirmBox);
        confirmBox.append(el("p", { id: "notify-summary", text: r.data.summary }), el("button", { class: "btn primary small", id: "notify-confirm", type: "button", text: "Yes, that is right: turn it on", on: { click: async () => {
          const c = await api("/api/notify/confirm", { method: "POST", body: { code: r.data.code } });
          if (c.ok) again(); else msg.textContent = (c.data && c.data.error) || "Could not confirm.";
        } } }));
      } } });
      const confirmBox = el("div", { id: "notify-confirm-box" });
      sec.append(el("p", { class: "muted", text: "Off. If you turn it on, EventMate emails you job updates, only to the address you confirm here, only between 09:00 and 20:00 IST, and never your budget. You choose what an email may contain; by default it is status and counts only." }),
        el("div", { class: "actions" }, to, ...opts.map((o) => el("label", { class: "muted" }, o.box, " " + o.t + " ")), review), confirmBox);
    }
    box.append(sec, msg);
    return box;
  }

  // Follow-through jobs (ADR-044): a goal such as "3 comparable caterer quotes by the deadline", kept moving across days. Every item waits for you.
  const named = (j, text) => Object.entries(j.names || {}).reduce((t, [id, n]) => t.split(id).join(n), text);  // an internal id is never shown: the name (and a test vendor's label) is
  function jobsView(jobs, saved, ctx) {
    const { again, fail, needName } = ctx;
    const box = el("section", { class: "alt", id: "jobs" }, el("b", { text: "Quote jobs" }),
      el("p", { class: "muted", text: "Ask several vendors for comparable quotes and let the agent keep it moving: it reads replies, proposes the prices it finds for you to confirm, and drafts a follow-up when a vendor is silent. A price becomes a recorded quote only when you confirm it and say how long it should count as valid. It never books or pays." }));
    for (const j of jobs) {
      const open = j.items.filter((i) => !i.resolved);
      box.append(el("div", { class: "alt job", "data-job": j.job_id },
        el("b", { text: j.goal }), " ", badge(j.status.replace("_", " "), j.status === "done" ? "ok" : j.status === "blocked" || j.status === "expired" ? "err" : "warn"),
        el("div", { class: "muted", text: j.next_action }),
        el("div", { class: "muted job-asking", text: "Asking: " + Object.values(j.names || {}).join("; ") }),
        ...open.map((i) => {
          const row = el("div", { class: "actions", "data-item": i.kind }, el("span", { text: named(j, i.text) }));
          const act = (label, path, body, cls = "btn small") => el("button", { class: cls, type: "button", text: label, on: { click: async () => { const r = await api(path, { method: "POST", body: body || {} }); if (r.ok) again(); else fail(r, "That did not work."); } } });
          if (i.kind === "approve_requests") row.append(act("Approve all requests", `/api/jobs/${j.job_id}/approve-requests`, {}, "btn primary small"));
          else if (i.kind === "confirm_claim") {
            const claim = j.claims.find((c) => c.claim_id === i.ref);
            const days = el("input", { class: "input claim-days", type: "number", min: "1", max: "90", placeholder: "valid for how many days?", value: claim && claim.stated_valid_days ? String(claim.stated_valid_days) : "", "aria-label": "Days this price is valid", style: { width: "11em" } });
            row.append(days, el("span", { class: "muted", text: claim && claim.stated_valid_days ? "the vendor wrote this validity" : "the vendor did not say; you choose" }),
              el("button", { class: "btn primary small confirm-claim", type: "button", text: "Confirm and record", on: { click: async () => {
                const n = parseInt(days.value, 10);
                if (!(n >= 1 && n <= 90)) { fail({ data: { error: "Enter how many days (1 to 90) this price should count as valid." } }, ""); return; }
                const r = await api(`/api/jobs/${j.job_id}/claims/${i.ref}/confirm`, { method: "POST", body: { valid_days: n } }); if (r.ok) again(); else fail(r, "Could not record it.");
              } } }), act("Not this one", `/api/jobs/${j.job_id}/claims/${i.ref}/reject`));
          } else if (i.kind === "approve_nudge") row.append(act("Approve follow-up", `/api/jobs/${j.job_id}/items/${i.item_id}/approve-nudge`), act("Skip", `/api/jobs/${j.job_id}/items/${i.item_id}/resolve`));
          else row.append(act("I dealt with it", `/api/jobs/${j.job_id}/items/${i.item_id}/resolve`));
          return row;
        }),
        ["done", "expired", "cancelled"].includes(j.status) ? null : el("button", { class: "btn small", type: "button", text: "Cancel this job", on: { click: async () => { const r = await api(`/api/jobs/${j.job_id}/cancel`, { method: "POST", body: {} }); if (r.ok) again(); else fail(r, "Could not cancel."); } } })));
    }
    if (!saved.length) { box.append(el("p", { class: "muted", id: "jobs-hint", text: "Save contacts for a few vendors above to start a job." })); return box; }
    const cat = el("select", { id: "j-cat", class: "input", "aria-label": "Category" }, ...["caterer", "venue", "photographer"].map((c) => el("option", { value: c, text: titleCase(c) })));
    const picks = saved.map((c) => ({ id: c.vendor_id, box: el("input", { type: "checkbox", value: c.vendor_id, "aria-label": c.vendor_id }) }));
    const min = el("input", { id: "j-min", class: "input", type: "number", min: "1", max: "10", value: "3", "aria-label": "How many quotes", style: { width: "5em" } });
    const cap = el("input", { id: "j-cap", class: "input", type: "number", min: "1", placeholder: "Cap in INR (optional)", "aria-label": "Cap in INR" });
    const days = el("select", { id: "j-days", class: "input", "aria-label": "Deadline" }, ...[[3, "in 3 days"], [7, "in 7 days"], [14, "in 14 days"]].map(([v, t]) => el("option", { value: v, text: t, selected: v === 7 })));
    box.append(el("div", { class: "actions" }, cat, el("span", { class: "muted", text: "quotes needed:" }), min, cap, days),
      el("div", { class: "actions" }, ...picks.map((p) => el("label", { class: "muted" }, p.box, " " + p.id + " "))),
      el("button", { class: "btn primary", id: "j-start", type: "button", text: "Draft the requests for this job", on: { click: async () => {
        const ids = picks.filter((p) => p.box.checked).map((p) => p.id);
        if (!needName()) return;
        if (!ids.length) { fail({ data: { error: "Tick the vendors to ask." } }, ""); return; }
        const n = parseInt(min.value, 10) || 1;
        const body = { category: cat.value, vendor_ids: ids, goal: `${n} comparable ${cat.value} quotes`, sender_name: state.senderName.trim(), deadline_days: parseInt(days.value, 10), min_quotes: n };
        if (parseInt(cap.value, 10) > 0) body.max_amount_inr = parseInt(cap.value, 10);
        const r = await api(`${ctx.planBase}/jobs`, { method: "POST", body }); if (r.ok) again(); else fail(r, "Could not start the job.");
      } } }));
    return box;
  }

  function threadView(t, ctx) {
    const { planBase, canSend, again, fail, needName } = ctx;
    const items = [...t.drafts.map((d) => ({ at: d.created_at, d })), ...t.replies.map((m) => ({ at: m.received_at, m }))].sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0));
    const origin = t.request.vendor_origin;
    const box = el("div", { class: "alt thread", "data-vendor": t.request.vendor_id }, el("b", { text: `${titleCase(t.request.category)}: ${t.request.vendor_name}` }), " ",
      origin === "seed" ? null : el("span", { class: "tag " + (origin === "test" ? "rej" : "eli"), "data-origin": origin, text: origin === "test" ? (state.labels && state.labels[t.request.vendor_id]) || "TEST VENDOR" : "Discovered" }), " ",
      el("span", { class: "muted", text: `${t.request.channel === "email" ? "email " + t.request.recipient : "WhatsApp +" + t.request.recipient}` }),
      t.unread ? el("span", { class: "tag eli", text: `${t.unread} new` }) : null);
    for (const it of items) {
      if (it.d) {
        const d = it.d, [label, kind] = STATUS_TAG[d.status] || [d.status, ""];
        box.append(el("div", { class: "msg out", "data-status": d.status },
          el("div", { class: "muted", text: `You${d.kind === "followup" ? " (follow-up)" : ""}: ` }), el("span", { class: "tag " + kind, text: label }), d.approved_by === "mandate" ? el("span", { class: "tag", text: "sent automatically" }) : null,
          el("pre", { class: "muted", style: { whiteSpace: "pre-wrap" }, text: d.body }),
          d.error ? el("div", { class: "bad", text: d.error + (d.status === "outcome_unknown" ? " Check the vendor chat before writing again." : "") }) : null,
          d.status === "prepared" ? el("div", { class: "actions" },
            canSend[d.channel] ? el("button", { class: "btn primary small approve", type: "button", text: "Approve and send", on: { click: async () => {
              const res = await api(`${planBase}/outreach/${d.draft_id}/approve`, { method: "POST", body: { content_hash: d.content_hash } });
              if (res.ok) toast(res.data.status === "sent" ? `Sent to ${d.vendor_name}.` : res.data.status === "approved" ? "Approved. It will go out as soon as the sending limits allow." : `Status: ${res.data.status}`, 5000); else fail(res, "Could not approve.");
              again();
            } } }) : null,
            el("button", { class: "btn small deny", type: "button", text: "Do not send", on: { click: async () => { const res = await api(`${planBase}/outreach/${d.draft_id}/deny`, { method: "POST", body: {} }); if (res.ok) again(); else fail(res, "Could not decline."); } } }),
            !canSend[d.channel] && safeLink(d.link) ? el("a", { class: "btn small", href: d.link, target: "_blank", rel: "noopener noreferrer", text: d.channel === "email" ? "Open in your email app" : "Open in WhatsApp" }) : null,
            !canSend[d.channel] ? el("button", { class: "btn small", type: "button", text: "I sent it", on: { click: async () => { const res = await api(`${planBase}/outreach/${d.draft_id}/sent`, { method: "POST", body: {} }); if (res.ok) again(); else fail(res, "Could not record that."); } } }) : null) : null));
      } else {
        const m = it.m, a = m.analysis;
        box.append(el("div", { class: "msg in", "data-unread": String(!m.read) },
          el("div", { class: "muted", text: `${t.request.vendor_name} replied ${new Date(m.received_at).toLocaleString()}` }), m.read ? null : el("span", { class: "tag eli", text: "new" }),
          m.sender_differs ? el("div", { class: "bad", text: `From ${m.sender}, not the address you wrote to. Read it before trusting anything in it.` }) : null,
          el("pre", { style: { whiteSpace: "pre-wrap" }, text: m.body || "(no text)" }),
          m.attachments ? el("div", { class: "muted", text: `${m.attachments} attachment${m.attachments === 1 ? "" : "s"} not opened` }) : null,
          a.says_unavailable ? el("div", { class: "bad", text: "Reads like they are not available on your date. Check their words." }) : a.says_available ? el("div", { class: "muted", text: "Reads like they are available. Check their words." }) : null,
          ...a.amounts.map((x) => el("div", { class: "actions" }, el("span", { class: "muted", text: `They wrote ${inr(x.low_inr)}${x.high_inr !== x.low_inr ? " to " + inr(x.high_inr) : ""} (${x.unit.replace("_", " ")}). Not recorded as a quote.` }),
            el("button", { class: "btn small to-quote", type: "button", text: "Record as quote…", on: { click: () => {
              state.prefillQuote = { vendor_id: m.vendor_id, amount: x.low_inr, basis: x.unit === "per_plate" ? "per_plate" : "total", evidence: `${m.channel} reply ${m.received_at.slice(0, 16)} (${m.external_id})`.slice(0, 200) };
              state.tab = "budget"; renderTabs(); renderPanels();
            } } }))),
          a.questions.length ? el("div", { class: "muted", text: "They asked: " + a.questions.join(" ") }) : null,
          m.read ? null : el("button", { class: "btn small", type: "button", text: "Mark read", on: { click: async () => { await api(`${planBase}/inbox/${m.message_id}/read`, { method: "POST", body: {} }); again(); } } })));
      }
    }
    if (t.request.status === "sent") {
      const text = el("textarea", { class: "input", maxlength: "1000", rows: "2", placeholder: "Your follow-up, in your own words", "aria-label": "Follow-up text" });
      const draftFollow = async (body) => {
        if (!needName()) return;
        const res = await api(`${planBase}/outreach/${t.request.draft_id}/followup`, { method: "POST", body: { text: body, sender_name: state.senderName.trim() } });
        if (res.ok) again(); else fail(res, "That follow-up was refused.");
      };
      const sug = el("div", { class: "chips" });
      box.append(el("div", { class: "actions" }, text,
        el("button", { class: "btn small", type: "button", text: "Draft follow-up", on: { click: () => text.value.trim() && draftFollow(text.value.trim()) } }),
        el("button", { class: "btn small", type: "button", text: "Suggest follow-ups", on: { click: async () => {
          const res = await api(`${planBase}/outreach/${t.request.draft_id}/suggestions`);
          clear(sug); if (!res.ok || !res.data.suggestions.length) { sug.append(el("span", { class: "muted", text: "Nothing to suggest until they reply." })); return; }
          for (const q of res.data.suggestions) sug.append(el("button", { class: "chip", type: "button", text: q, on: { click: () => draftFollow(q) } }));
        } } })), sug);
    }
    return box;
  }

  // Advertised prices you bookmarked from Explore. Not quotes: they never change this plan's prices.
  async function renderLeads(card) {
    const r = await api(`/api/plans/${encodeURIComponent(state.planId)}/leads`);
    for (const n of [...card.children].slice(1)) n.remove();
    if (!r.ok) { card.append(el("p", { class: "muted", text: "Leads could not be loaded." })); return; }
    if (!r.data.length) { card.append(el("p", { class: "muted", id: "leads-empty", text: "None yet. Use Explore deals and trips, then Save as lead." })); return; }
    card.append(el("p", { class: "muted", text: r.data[0].notice }),
      ...r.data.map((l) => el("div", { class: "alt lead" }, el("b", { text: `${titleCase(l.kind)}: ${l.label || l.domain}` }), el("span", { class: "tag", text: `${l.low_inr === l.high_inr ? inr(l.low_inr) : inr(l.low_inr) + " to " + inr(l.high_inr)} ${titleCase(l.unit)}` }), " ", el("a", { href: l.url, target: "_blank", rel: "noopener noreferrer nofollow", text: l.domain }))));
  }

  // Quotes the user was given. They set the plan's prices; a vendor's confirmation needs an evidence reference.
  async function renderQuotes(card) {
    const r = await api(`/api/plans/${state.planId}/quotes`);
    for (const n of [...card.children].slice(1)) n.remove();
    if (!r.ok) { card.append(el("p", { class: "muted", text: "Quotes could not be loaded." })); return; }
    const rows = r.data;
    const stale = rows.filter((x) => x.price_stale);
    const list = el("ul", { class: "alts", id: "q-list" });
    for (const x of rows) {
      const rep = x.report;
      list.append(el("li", { class: "alt" }, el("b", { text: `${titleCase(x.category)}: ${x.vendor_name}` }),
        el("span", { class: "tag " + (rep && rep.status === "expired" ? "rej" : "eli"), text: rep ? rep.status : "no quote" }),
        el("div", { class: "muted", text: `Plan ${inr(x.plan_cost)}; price now ${inr(x.current_price)}` + (rep ? `; quoted ${inr(rep.effective_total)} as given, ${rep.source_type.replace("_", " ")}, valid until ${rep.expires_at}` : "; seed estimate, no quote recorded") }),
        ...(rep ? rep.flags.map((f) => el("div", { class: "muted", text: "• " + f })) : [])));
    }
    const msg = el("div", { class: "muted", id: "q-msg", role: "status" });
    const f = {
      vendor: el("select", { id: "q-vendor", class: "input", "aria-label": "Vendor" }, ...rows.map((x) => el("option", { value: x.vendor_id, text: `${titleCase(x.category)}: ${x.vendor_name}` }))),
      source: el("select", { id: "q-source", class: "input", "aria-label": "Where the quote came from" }, ...[["user_input", "Told to me"], ["vendor_quote", "Vendor's written quote"], ["vendor_confirmation", "Vendor's written confirmation"], ["public_listing", "Public listing (never sets a price)"]].map(([v, t]) => el("option", { value: v, text: t }))),
      amount: el("input", { id: "q-amount", class: "input", type: "number", min: "1", step: "1", placeholder: "Amount (INR)", "aria-label": "Amount in INR" }),
      basis: el("select", { id: "q-basis", class: "input", "aria-label": "Basis" }, el("option", { value: "total", text: "Total" }), el("option", { value: "per_plate", text: "Per plate" })),
      guests: el("input", { id: "q-guests", class: "input", type: "number", min: "1", placeholder: "Guests quoted for (per plate)", "aria-label": "Guests the quote was for" }),
      expires: el("input", { id: "q-expires", class: "input", type: "date", "aria-label": "Quote valid until" }),
      by: el("input", { id: "q-by", class: "input", maxlength: "80", placeholder: "Your name", "aria-label": "Entered by" }),
      evidence: el("input", { id: "q-evidence", class: "input", maxlength: "200", placeholder: "Evidence (email subject, message id)", "aria-label": "Evidence reference" }),
      hidden: el("input", { id: "q-hidden", class: "input", maxlength: "80", placeholder: "Hidden cost label (optional)", "aria-label": "Hidden cost label" }),
      hiddenAmt: el("input", { id: "q-hidden-amt", class: "input", type: "number", min: "1", placeholder: "Hidden cost INR (optional)", "aria-label": "Hidden cost amount" }),
    };
    if (state.prefillQuote && rows.some((x) => x.vendor_id === state.prefillQuote.vendor_id)) {
      const pq = state.prefillQuote; state.prefillQuote = null;
      f.vendor.value = pq.vendor_id; f.source.value = "vendor_quote"; f.amount.value = String(pq.amount); f.basis.value = pq.basis; f.evidence.value = pq.evidence;
      msg.textContent = "Filled from the vendor's reply. Check the amount, the quote's validity date and your name, then record it.";
    }
    const add = el("button", { class: "btn primary", id: "q-add", type: "button", text: "Record quote", on: { click: async () => {
      const x = rows.find((v) => v.vendor_id === f.vendor.value), n = parseInt(f.amount.value, 10);
      if (!(n > 0) || !f.expires.value || !f.by.value.trim()) { msg.textContent = "Enter an amount, who you are and the date the quote is valid until."; return; }
      const body = { category: x.category, source_type: f.source.value, entered_by: f.by.value.trim(), expires_at: f.expires.value, confidence: f.source.value === "user_input" ? 0.6 : 0.9,
        evidence_ref: f.evidence.value.trim(), amount_inr: n, basis: f.basis.value, guest_count: f.basis.value === "per_plate" ? parseInt(f.guests.value, 10) || null : null,
        hidden_costs: f.hidden.value.trim() ? [{ label: f.hidden.value.trim(), amount_inr: parseInt(f.hiddenAmt.value, 10) || null }] : [] };
      const res = await api(`/api/observations/${f.vendor.value}/quotes`, { method: "POST", body });
      if (res.ok) renderQuotes(card); else msg.textContent = (res.data && res.data.details && res.data.details[0] && res.data.details[0].message) || (res.data && res.data.error) || "That quote was refused.";
    } } });
    const reprice = el("button", { class: "btn", id: "q-reprice", type: "button", text: "Re-price this plan", on: { click: async () => {
      const res = await api(`/api/plans/${state.planId}/replan`, { method: "POST", body: { kind: "budget_change", new_budget: state.plan.request.budget_total } });
      if (res.ok && res.data.plan) { await refreshList(); await selectPlan(state.planId, null, "budget"); toast(`Plan re-priced (version ${res.data.plan.version}).`); }
      else toast((res.data && res.data.violations && res.data.violations[0].detail) || "Could not re-price the plan.", 5000);
    } } });
    card.append(
      el("p", { class: "muted", text: "Quotes you were given set this plan's prices. An expired quote is not used: the vendor is dropped until you record a fresh one. Nothing is sent to vendors." }),
      stale.length ? el("div", { class: "bad", id: "q-stale" }, el("p", { text: "Recorded quotes differ from this plan's prices: " + stale.map((x) => `${titleCase(x.category)} ${inr(x.plan_cost)} → ${inr(x.current_price)}`).join("; ") + "." }), reprice) : null,
      list,
      el("div", { class: "actions" }, f.vendor, f.source, f.amount, f.basis, f.guests, f.expires),
      el("div", { class: "actions" }, f.by, f.evidence, f.hidden, f.hiddenAmt, add), msg);
  }

  // Money the user reports. EventMate records it; it never pays anything.
  async function renderLedger(card) {
    const r = await api(`/api/plans/${state.planId}/ledger`);
    for (const n of [...card.children].slice(1)) n.remove();
    if (!r.ok) { card.append(el("p", { class: "muted", text: "The ledger could not be loaded." })); return; }
    const s = r.data.summary;
    const rows = s.lines.map((l) => el("tr", {}, el("td", { text: titleCase(l.category) }), el("td", { class: "num", text: inr(l.planned) }), el("td", { class: "num", text: l.quoted === null ? "-" : inr(l.quoted) }),
      el("td", { class: "num", text: inr(l.paid) }), el("td", { class: "num", text: inr(l.pending) }), el("td", { class: "num ledger-variance", text: (l.variance > 0 ? "+" : "") + inr(l.variance) })));
    const category = el("select", { id: "l-category", class: "input", "aria-label": "Category" }, ...s.lines.map((l) => el("option", { value: l.category, text: titleCase(l.category) })));
    const kind = el("select", { id: "l-kind", class: "input", "aria-label": "Type" }, ...["quote", "payment", "refund"].map((k) => el("option", { value: k, text: titleCase(k) })));
    const amount = el("input", { id: "l-amount", class: "input", type: "number", min: "1", step: "1", placeholder: "Amount (INR)", "aria-label": "Amount in INR" });
    const note = el("input", { id: "l-note", class: "input", maxlength: "200", placeholder: "Note (optional)", "aria-label": "Note" });
    const msg = el("div", { class: "muted", id: "l-msg", role: "status" });
    const add = el("button", { class: "btn primary", id: "l-add", type: "button", text: "Record", on: { click: async () => {
      const n = parseInt(amount.value, 10);
      if (!(n > 0)) { msg.textContent = "Enter a whole amount above zero."; return; }
      const res = await api(`/api/plans/${state.planId}/ledger`, { method: "POST", body: { kind: kind.value, category: category.value, amount: n, note: note.value } });
      if (res.ok) renderLedger(card); else msg.textContent = (res.data && res.data.error) || "That entry was refused.";
    } } });
    card.append(
      el("p", { class: "muted", text: "Amounts you report. EventMate records them and never makes a payment. The latest quote replaces the planned figure." }),
      el("table", { class: "tbl" }, el("thead", {}, el("tr", {}, el("th", { text: "Category" }), el("th", { class: "num", text: "Planned" }), el("th", { class: "num", text: "Quoted" }), el("th", { class: "num", text: "Paid" }), el("th", { class: "num", text: "Pending" }), el("th", { class: "num", text: "Variance" }))), el("tbody", {}, ...rows),
        el("tfoot", {}, el("tr", {}, el("th", { text: "Total" }), el("th", { class: "num", text: inr(s.planned_total) }), el("th", { class: "num", text: inr(s.quoted_total) }), el("th", { class: "num", id: "l-paid-total", text: inr(s.paid_total) }), el("th", { class: "num", text: inr(s.pending_total) }), el("th", { class: "num", text: (s.variance_total > 0 ? "+" : "") + inr(s.variance_total) })))),
      el("p", { class: s.headroom < 0 ? "bad" : "muted", text: `Committed ${inr(s.committed_total)} of ${inr(s.budget_total)}; ${s.headroom < 0 ? "over budget by " + inr(-s.headroom) : "headroom " + inr(s.headroom)}.` }),
      Object.keys(s.orphaned_paid).length ? el("p", { class: "bad", id: "l-orphans", text: "Paid to services no longer in this plan: " + Object.entries(s.orphaned_paid).map(([c, n]) => `${titleCase(c)} ${inr(n)}`).join(", ") + ". Ask the vendor about a refund." }) : null,
      el("div", { class: "actions" }, category, kind, amount, note, add), msg);
  }

  // ------------------------------------------------------------ changes
  async function panelChanges(root) {
    const vers = el("div", { class: "versions" });
    for (const v of [...state.versions].reverse()) {
      vers.append(el("div", { class: "ver", "aria-current": String(v.version === state.version) },
        el("div", { class: "n", text: "v" + v.version }),
        el("div", {}, el("b", { text: v.change_reason }), el("div", { class: "muted", text: `${new Date(v.created_at).toLocaleString("en-GB")} · ${v.guest_count} guests · ${inr(v.total_cost)} of ${inr(v.budget_total)} · ${v.event_date}` })),
        el("div", { class: "actions" }, el("button", { class: "btn small", type: "button", text: "Open", on: { click: () => selectPlan(state.planId, v.version, "changes") } }),
          v.parent_version ? el("button", { class: "btn small ghost", type: "button", text: "Diff from v" + v.parent_version, on: { click: () => showDiff(v.parent_version, v.version) } }) : null)));
    }
    root.append(el("section", { class: "card" }, el("h2", { text: "Version history (immutable)" }), el("p", { class: "muted", text: "Every change writes a new read-only version. Earlier versions are never edited." }), vers),
      el("section", { class: "card", id: "diff-card" }, el("h2", { text: "What changed" }), el("div", { id: "diff-body" })),
      el("div", { class: "actions" }, el("button", { class: "btn", type: "button", text: "Report a disruption…", on: { click: () => openReplan("budget_change") } })));
    const cur = state.versions.find((v) => v.version === state.version);
    if (cur && cur.parent_version) showDiff(cur.parent_version, cur.version); else $("#diff-body").append(el("p", { class: "muted", text: "This is the first version: nothing to compare yet." }));
  }
  async function showDiff(a, b) {
    const body = $("#diff-body"); if (!body) return;
    const r = await api(`/api/plans/${state.planId}/diff?old=${a}&new=${b}`);
    clear(body);
    if (!r.ok) { body.append(el("p", { class: "muted", text: "Could not load the diff." })); return; }
    body.append(el("p", { class: "muted", text: `Version ${a} to version ${b}` }), renderDiff(r.data));
  }
  function renderDiff(d) {
    const line = (i, sign) => el("li", {}, el("b", { text: `${titleCase(i.kind)} · ${i.key.replace(/_/g, " ")}` }), sign === "changed" ? el("small", {}, i.before || "-", el("span", { class: "arrow", text: "  →  " }), i.after || "-") : el("small", { text: i.before || i.after || "" }), i.note ? el("small", { class: "muted", text: i.note }) : null);
    const wrap = el("div", { class: "diff" });
    for (const [k, label] of [["added", "Added"], ["removed", "Removed"], ["changed", "Changed"], ["preserved", "Preserved"]]) wrap.append(el("div", { class: "dcol " + k }, el("h4", {}, el("span", { text: label }), el("span", { class: "mono", text: String(d[k].length) })), el("ul", {}, ...(d[k].length ? d[k].map((i) => line(i, k)) : [el("li", { class: "muted", text: "none" })]))));
    return wrap;
  }

  // -------------------------------------------------------------- audit
  async function panelAudit(root) {
    const card = el("section", { class: "card" }, el("h2", { text: "Decision log for this version" }), el("p", { class: "muted", text: "Every material decision with alternatives, constraints checked, justification, confidence and source." }));
    const filters = el("div", { class: "filters" }), list = el("div", { class: "entries" });
    card.append(filters, list); root.append(card);
    if (!state.decisions) { const r = await api(`/api/plans/${state.planId}/decisions?version=${state.version}`); state.decisions = r.ok ? r.data : []; }
    const agents = ["all", ...new Set(state.decisions.map((e) => e.agent))];
    const draw = (agent) => {
      clear(filters); for (const a of agents) filters.append(el("button", { class: "chip", type: "button", "aria-pressed": String(a === agent), text: a, on: { click: () => draw(a) } }));
      clear(list);
      for (const e of state.decisions.filter((x) => agent === "all" || x.agent === agent)) list.append(entryCard(e));
      if (!list.firstChild) list.append(el("p", { class: "muted", text: "No entries recorded for this version." }));
    };
    draw("all");
  }
  function entryCard(e) {
    const failed = e.constraints_checked.filter((c) => !c.passed).length;
    return el("article", { class: "entry" },
      el("header", {}, el("span", { class: "agent " + e.agent, text: e.agent }), badge(e.decision_type, e.decision_type === "failure" ? "err" : ""), el("span", { class: "conf", title: "Confidence" }, "confidence " + Math.round(e.confidence * 100) + "%", el("span", { class: "meter" }, el("i", { style: { width: e.confidence * 100 + "%" } }))), el("span", { class: "src", text: e.source })),
      el("b", { text: e.summary }), el("p", { class: "muted", text: e.justification }),
      e.alternatives.length ? el("details", { class: "fold" }, el("summary", { text: `Alternatives (${e.alternatives.length})` }), el("ul", { class: "alts" }, ...e.alternatives.map((a) => el("li", { class: "alt" }, el("b", { text: a.option }), " " + a.outcome, el("div", { class: "muted", text: a.reason_not_chosen }))))) : (e.no_alternative_reason ? el("p", { class: "muted", text: "No alternative: " + e.no_alternative_reason }) : null),
      e.constraints_checked.length ? el("details", { class: "fold" }, el("summary", { text: `Constraints checked (${e.constraints_checked.length}${failed ? ", " + failed + " failed" : ""})` }), el("div", { class: "checks" }, ...e.constraints_checked.map((c) => el("div", { class: "check" + (c.passed ? "" : " fail") }, el("span", { class: "tick", text: c.passed ? "✓" : "✗" }), el("div", {}, el("code", { text: c.name }), " ", el("span", { class: "why", text: c.detail })))))) : null);
  }

  // ------------------------------------------------------------- replan
  const KINDS = [["vendor_cancellation", "Vendor cancelled"], ["budget_change", "Budget change"], ["guest_count_change", "Guest count"], ["date_change", "Date change"]];
  let replanKind = "budget_change", replanVendor = null, pendingOverride = [];
  function openReplan(kind, vendorId) {
    replanKind = kind || "budget_change"; replanVendor = vendorId || null; pendingOverride = [];
    const grp = clear($("#kind-group")); grp.append(el("legend", { class: "sr", text: "Kind of change" }));
    for (const [k, label] of KINDS) grp.append(el("label", {}, el("input", { type: "radio", name: "kind", value: k, checked: k === replanKind, on: { change: () => { replanKind = k; drawKindFields(); } } }), el("span", { text: label })));
    clear($("#replan-result")); drawKindFields(); drawOverrides();
    const dlg = $("#replan-dialog"); if (!dlg.open) dlg.showModal();
  }
  function drawKindFields() {
    const p = state.plan, box = clear($("#kind-fields")), r = p.request;
    const field = (id, label, input) => el("div", { class: "fieldrow" }, el("label", { for: id, text: label }), input);
    if (replanKind === "vendor_cancellation") {
      const s = el("select", { id: "f-vendor", class: "select" }); for (const sel of p.selections) s.append(el("option", { value: sel.vendor_id, text: `${titleCase(sel.category)}: ${sel.name}${sel.booking_status === "confirmed" ? " (confirmed)" : ""}`, selected: sel.vendor_id === replanVendor }));
      box.append(field("f-vendor", "Which vendor cancelled?", s));
    } else if (replanKind === "budget_change") box.append(field("f-budget", "New total budget (INR)", el("input", { id: "f-budget", class: "input", type: "number", min: "1", step: "1", value: String(r.budget_total) })));
    else if (replanKind === "guest_count_change") box.append(field("f-guests", "New guest count", el("input", { id: "f-guests", class: "input", type: "number", min: "1", max: "100000", step: "1", value: String(r.guest_count) })));
    else box.append(field("f-date", "New event date", el("input", { id: "f-date", class: "input", type: "date", min: state.today, value: r.event_date })));
  }
  function drawOverrides() {
    const box = $("#override-box"), confirmed = state.plan.selections.filter((s) => s.booking_status === "confirmed");
    clear(box); box.hidden = confirmed.length === 0; if (!confirmed.length) return;
    box.append(el("b", { text: "Confirmed bookings are kept unless you release them:" }));
    for (const s of confirmed) box.append(el("label", { style: { display: "block", marginTop: "6px" } }, el("input", { type: "checkbox", value: s.vendor_id, checked: pendingOverride.includes(s.vendor_id), on: { change: (ev) => { pendingOverride = ev.target.checked ? [...pendingOverride, s.vendor_id] : pendingOverride.filter((x) => x !== s.vendor_id); } } }), " Release " + s.name));
  }
  function disruptionBody() {
    const b = { kind: replanKind, override_confirmed: pendingOverride };
    if (replanKind === "vendor_cancellation") b.vendor_id = $("#f-vendor").value;
    else if (replanKind === "budget_change") b.new_budget = parseInt($("#f-budget").value, 10);
    else if (replanKind === "guest_count_change") b.new_guest_count = parseInt($("#f-guests").value, 10);
    else b.new_date = $("#f-date").value;
    return b;
  }
  async function submitReplan(ev) {
    ev.preventDefault();
    const out = clear($("#replan-result")), btn = $("#replan-submit"); btn.disabled = true; btn.textContent = "Replanning…";
    try {
      const r = await api(`/api/plans/${state.planId}/replan`, { method: "POST", body: disruptionBody() });
      if (r.ok && r.data.plan) { $("#replan-dialog").close(); await refreshList(); await selectPlan(state.planId, null, "changes"); toast(`Replanned: version ${r.data.plan.version}. Earlier versions are untouched.`, 4500); return; }
      if (r.status === 409 && r.data) {
        const box = el("div", { class: "notice warn" }); out.append(box); showViolations(box, "This needs your decision. Nothing was changed:", r.data.violations, []);
        const ids = r.data.violations.map((v) => (/override_confirmed=\['([A-Za-z0-9_-]+)'\]/.exec(v.repair) || [])[1]).filter(Boolean);
        if (ids.length) { box.append(el("button", { class: "btn small danger", type: "button", style: { marginTop: "10px" }, text: "Release the confirmed booking and replan", on: { click: () => { pendingOverride = [...new Set([...pendingOverride, ...ids])]; drawOverrides(); $("#replan-form").requestSubmit(); } } })); }
      } else if (r.data && (r.data.violations || r.data.details)) { const box = el("div", { class: "notice err" }); out.append(box); showViolations(box, "No valid plan after that change. The previous version is still valid:", r.data.violations || [], (r.data.details || []).map((d) => `${d.field}: ${d.message}`)); }
      else out.append(el("div", { class: "notice err", text: (r.data && r.data.error) || "Something went wrong." }));
    } catch (e) { out.append(el("div", { class: "notice err", text: e.message })); }
    finally { btn.disabled = false; btn.textContent = "Replan"; }
  }

  // --------------------------------------------------------------- boot

  // ------------------------------------------------- conversational intake
  // The AI only proposes. Every value is shown on the review card, editable, and nothing is planned until the user confirms.
  const CHAT_EXAMPLES = [
    ["Engagement, Pune", "We're planning a vegetarian engagement in Pune on Dec 12, around 120 people, budget 2 lakh"],
    ["Small birthday", "40 guests birthday in Pune on December 12, 90k budget, need catering and a photographer"],
  ];
  const FIELD_LABEL = { guest_count: "guest count", city: "city", event_date: "date", budget_total: "budget" };
  const OPTIONS = {
    event_type: ["engagement_party", "wedding", "birthday", "corporate", "conference", "community", "other"],
    venue_setting: ["any", "indoor", "outdoor"],
    dietary: ["vegetarian", "non_vegetarian", "vegan", "jain", "halal", "gluten_free"],
    services: ["caterer", "photographer"],
    reminders: ["invitation", "vendor_confirmation"],
  };
  const chat = { messages: [], draft: {}, flags: new Set(), source: "rule:parser", busy: false, editing: null };

  function say(kind, text) {
    const log = $("#chat-log"); log.append(el("div", { class: "bubble " + kind, text })); log.scrollTop = log.scrollHeight;
  }
  const missingFields = () => ["guest_count", "city", "event_date", "budget_total"].filter((f) => chat.draft[f] === null || chat.draft[f] === undefined);

  async function sendChat(text) {
    if (chat.busy || !text.trim()) return;
    chat.busy = true; $("#chat-send").disabled = true; $("#plan-errors").hidden = true;
    say("me", text); chat.messages.push(text);
    const r = await api("/api/intake", { method: "POST", body: { messages: chat.messages.slice(-20), draft: chat.draft } });
    chat.busy = false; $("#chat-send").disabled = false;
    if (!r.ok) { say("note", (r.data && (r.data.error || (r.data.problems || []).join("; "))) || "I could not read that. Try again."); return; }
    chat.draft = r.data.draft; chat.flags = new Set(r.data.flags); chat.source = r.data.source;
    for (const n of r.data.notes) say("note", n);
    say("ai", r.data.question || "That's everything I need. Check the summary below, fix anything that is off, then confirm.");
    for (const c of r.data.choices) offerChoice(c);
    renderReview();
  }

  // Two values for one fact: one tap settles it. The server never picks for the user.
  function offerChoice(c) {
    const row = el("div", { class: "bubble ai choices" });
    for (const o of c.options) {
      const text = c.field === "budget_total" ? inr(o) : c.field === "event_date" ? fmtDate(new Date(o + "T00:00:00")) : o + " guests";
      row.append(el("button", { type: "button", class: "chip", text, on: { click: () => {
        chat.draft = { ...chat.draft, [c.field]: c.field === "event_date" ? o : parseInt(o, 10) }; chat.flags.delete(c.field);
        for (const b of row.querySelectorAll("button")) b.disabled = true; say("me", text); renderReview();
      } } }));
    }
    $("#chat-log").append(row); $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
  }

  // One sentence instead of a form: click any highlighted value to change it.
  function renderReview() {
    const box = clear($("#review")), d = chat.draft;
    box.hidden = false;
    const val = (field, text, key = field) => {
      const todo = text === null;
      return el("button", { type: "button", class: "val" + (todo ? " todo" : "") + (chat.flags.has(key) ? " check" : ""), "data-field": field, title: chat.flags.has(key) ? "Inferred from your words, not stated outright. Please check it." : "Click to change", text: todo ? "+ " + FIELD_LABEL[field] : text, on: { click: () => editField(field) } });
    };
    const label = (s) => titleCase(s);
    const line = el("div", {},
      el("span", { class: "lead", text: "Please review: nothing is planned until you confirm" }),
      "A ", val("event_type", d.event_type ? label(d.event_type) : "event"), " for ", val("guest_count", d.guest_count ? d.guest_count + " guests" : null),
      " in ", val("city", d.city || null), " on ", val("event_date", d.event_date ? fmtDate(new Date(d.event_date + "T00:00:00")) : null),
      ", budget ", val("budget_total", d.budget_total ? inr(d.budget_total) : null), ".");
    const toggles = el("div", { class: "toggles" });
    for (const [field, title] of [["dietary", "Diet"], ["services", "Also book"], ["reminders", "Remind"]]) {
      toggles.append(el("span", { class: "muted", text: title + ":" }));
      for (const o of OPTIONS[field]) {
        const on = (d[field] || []).includes(o);
        toggles.append(el("button", { type: "button", class: "val " + (on ? "on" : "off"), "aria-pressed": on ? "true" : "false", text: label(o), on: { click: () => { const cur = new Set(d[field] || []); if (cur.has(o)) cur.delete(o); else cur.add(o); chat.draft = { ...d, [field]: [...cur] }; chat.flags.delete(field); renderReview(); } } }));
      }
    }
    const setting = el("select", { class: "select", "aria-label": "Venue setting", on: { change: (ev) => { chat.draft = { ...d, venue_setting: ev.target.value === "any" ? null : ev.target.value }; chat.flags.delete("venue_setting"); } } },
      ...OPTIONS.venue_setting.map((o) => el("option", { value: o, text: label(o) + (o === "any" ? " venue" : ""), selected: (d.venue_setting || "any") === o })));
    let subtype = null;
    if (d.event_type === "community") {
      if (!chat.subtypes) { chat.subtypes = []; api("/api/community/profiles").then((r) => { if (r.ok) { chat.subtypes = r.data.profiles; chat.defaultSubtype = r.data.default; renderReview(); } }); }
      subtype = el("select", { class: "select", id: "review-subtype", "aria-label": "Kind of community event", on: { change: (ev) => { chat.draft = { ...d, community_subtype: ev.target.value }; } } },
        ...chat.subtypes.map((o) => el("option", { value: o.key, text: o.label, selected: (d.community_subtype || chat.defaultSubtype) === o.key })));
    }
    const gl = el("select", { class: "select", id: "review-guest-list", "aria-label": "Guest list" }, el("option", { value: "", text: "No guest list" }), ...[...$("#guest-list").options].slice(1).map((o) => el("option", { value: o.value, text: o.textContent })));
    const still = missingFields();
    const confirm = el("button", { class: "btn primary", type: "button", disabled: still.length > 0 || chat.busy, text: still.length ? "Still need: " + still.map((f) => FIELD_LABEL[f]).join(", ") : "Looks right, build the plan", on: { click: confirmIntake } });
    box.append(line, toggles, el("div", { class: "actions" }, setting, subtype, gl, confirm));
    if (chat.flags.size) box.append(el("span", { class: "flagnote", text: "Highlighted in amber: inferred from your words, not stated outright. Please check them." }));
  }

  // Inline edit: the chip turns into a single input, Enter keeps it, Escape cancels.
  function editField(field) {
    const d = chat.draft, type = { guest_count: "number", budget_total: "number", event_date: "date" }[field] || (field === "event_type" ? "select" : "text");
    const cur = d[field] ?? "";
    const input = type === "select" ? el("select", { class: "select inline", "aria-label": field }, ...OPTIONS.event_type.map((o) => el("option", { value: o, text: titleCase(o), selected: o === cur }))) : el("input", { class: "input inline", type, value: cur, "aria-label": FIELD_LABEL[field] || field, min: type === "number" ? "1" : undefined });
    let finished = false;
    const done = (save) => {
      if (finished) return; finished = true;
      if (save) { const raw = input.value.trim(); const v = raw === "" ? null : type === "number" ? parseInt(raw, 10) : raw; if (v === null || !Number.isNaN(v)) { chat.draft = { ...chat.draft, [field]: v }; chat.flags.delete(field); } }
      renderReview();
    };
    input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); done(true); } else if (ev.key === "Escape") done(false); });
    input.addEventListener("blur", () => done(true));
    $(`#review [data-field="${field}"]`).replaceWith(input); input.focus();
  }

  async function confirmIntake() {
    const errs = clear($("#plan-errors")); errs.hidden = true;
    const btn = $("#review .btn.primary"); btn.disabled = true; btn.textContent = "Planning, solving and validating…";
    const body = { draft: chat.draft, raw_text: chat.messages.join(" ").slice(0, 2000), source: chat.source };
    const gl = $("#review-guest-list").value; if (gl) body.guest_list_id = gl;
    try {
      const r = await api("/api/intake/confirm", { method: "POST", body });
      if (r.status === 201) {
        state.tasks = r.data.tasks; await refreshList(); await selectPlan(r.data.plan.plan_id, null, "overview"); toast("Plan validated and saved as version 1.");
        const v = verification(state.plan);
        say("ai", v.state === "verified" ? "Done. The plan passed every hard check; see it on the right." : `Done, as a provisional plan. Every check that could be decided passed, but it is not fully verified. ${v.label}. ${v.items.map((i) => i.repair).join(" ")}`); $("#review").hidden = true; chat.messages = []; chat.draft = {}; chat.flags = new Set();
        showReality(errs, r.data.reality);
      } else if (r.status === 422 && r.data && r.data.violations) { showViolations(errs, "No plan can satisfy every hard constraint. Change a value above and confirm again:", r.data.violations, []); showReality(errs, r.data.reality); renderReview(); }
      else { showViolations(errs, (r.data && r.data.error) || "Something went wrong.", [], []); renderReview(); }
    } catch (e) { showViolations(errs, e.message, [], []); renderReview(); }
  }

  function initChat() {
    const box = $("#chat-examples");
    for (const [label, text] of CHAT_EXAMPLES) box.append(el("button", { class: "chip", type: "button", text: label, on: { click: () => { $("#chat-input").value = text; $("#chat-input").focus(); } } }));
    $("#chat-form").addEventListener("submit", (ev) => { ev.preventDefault(); const i = $("#chat-input"); const t = i.value; i.value = ""; sendChat(t); });
    say("ai", "Hi! Tell me about your event in your own words: what, where, when, how many people and your budget. I'll fill in the rest with you.");
  }

  async function boot() {
    initTheme(); renderExamples(); initChat();
    $("#plan-form").addEventListener("submit", onSubmitPlan);
    $("#replan-form").addEventListener("submit", submitReplan);
    $("#replan-close").addEventListener("click", () => $("#replan-dialog").close());
    $("#version-select").addEventListener("change", (ev) => selectPlan(state.planId, parseInt(ev.target.value, 10), state.tab));
    const s = await api("/api/session");
    if (!s.ok) { toast("Cannot start: " + ((s.data && s.data.error) || "server unreachable"), 8000); return; }
    state.csrf = s.data.csrf; state.today = s.data.today; state.roles = s.data.roles || [];
    const gl = $("#guest-list"); for (const id of s.data.guest_lists) gl.append(el("option", { value: id, text: id }));
    await refreshList();
    if (state.plans.length) await selectPlan(state.plans[0].plan_id);
  }
  // ------------------------------------------------------------- explore
  // Deals, places, trips and weather. Everything shown comes from a tool and is labelled as such; nothing here books or pays.
  const xnum = (v) => (v === "" || v == null ? null : parseInt(v, 10));
  const xfld = (id, label, props = {}) => el("label", { class: "fld" }, el("span", { text: label }), el("input", Object.assign({ id, class: "input", autocomplete: "off" }, props)));
  const xsel = (id, label, opts) => el("label", { class: "fld" }, el("span", { text: label }), el("select", { id, class: "select" }, ...opts.map(([v, t]) => el("option", { value: v, text: t }))));
  const SERVICE_KINDS = [["caterer", "Caterer"], ["venue", "Venue"], ["photographer", "Photographer"], ["hotel", "Hotel"]];
  const errText = (r) => (r.data && r.data.details && r.data.details[0] && r.data.details[0].message) || (r.data && r.data.error) || "Something went wrong.";

  function openExplore() {
    state.exploring = true;
    $("#empty-state").hidden = true; $("#plan-view").hidden = true;
    const host = clear($("#explore-view")); host.hidden = false;
    const out = el("div", { id: "x-out", "aria-live": "polite" });
    const msg = el("p", { class: "muted", id: "x-msg", role: "status" });
    const show = (node) => { clear(out); out.append(node); };
    const run = async (button, path, body, render, method = "POST") => {
      button.disabled = true; msg.textContent = "Working…";
      const r = await api(path, method === "GET" ? {} : { method, body });
      button.disabled = false; msg.textContent = "";
      if (!r.ok) { show(el("div", { class: "notice err", id: "x-error", text: errText(r) })); return; }
      show(render(r.data));
    };
    const free = el("input", { id: "x-text", class: "input", maxlength: "600", placeholder: "e.g. cheapest caterers in Pune for 120 guests, or Delhi to Mumbai 3 nights then Goa 2 nights from 12 Dec" });
    const goFree = el("button", { class: "btn primary", id: "x-go", type: "submit", text: "Search" });
    const freeForm = el("form", { class: "chat-form", autocomplete: "off", on: { submit: (ev) => { ev.preventDefault(); if (!free.value.trim()) return; run(goFree, "/api/explore", { text: free.value.trim() }, (d) => renderRouted(d)); } } }, free, goFree);
    const status = el("p", { class: "muted", id: "x-status" });
    api("/api/explore/status").then((r) => { status.textContent = r.ok && r.data.configured ? "Type what you need in your own words, or use the forms below." : "Typing in your own words needs a model key (NVIDIA_API_KEY or OPENROUTER_API_KEY in .env). The forms below work without one."; });

    const dKind = xsel("d-kind", "Service", SERVICE_KINDS), dCity =xfld("d-city", "City", { value: "Pune" }), dGuests =xfld("d-guests", "Guests", { type: "number", min: "1" }), dBudget =xfld("d-budget", "Budget (INR)", { type: "number", min: "1" }), dNights = xfld("d-nights", "Nights (hotels)", { type: "number", min: "1", max: "60" });
    const dGo = el("button", { class: "btn primary", id: "d-go", type: "button", text: "Find deals", on: { click: () => run(dGo, "/api/deals", { kind: $("#d-kind").value, city: $("#d-city").value, guests: xnum($("#d-guests").value), nights: xnum($("#d-nights").value), budget_inr: xnum($("#d-budget").value) }, renderDeals) } });
    const pKind = xsel("p-kind", "Service", SERVICE_KINDS), pCity =xfld("p-city", "City", { value: "Pune" });
    const pRev = el("label", { class: "fld" }, el("input", { id: "p-rev", type: "checkbox" }), el("span", { text: " Include reviews (uses more of the Google quota)" }));
    const pGo = el("button", { class: "btn", id: "p-go", type: "button", text: "Find rated places", on: { click: () => run(pGo, `/api/places?kind=${encodeURIComponent($("#p-kind").value)}&city=${encodeURIComponent($("#p-city").value)}&reviews=${$("#p-rev").checked ? 1 : 0}`, null, renderPlaces, "GET") } });
    const tFrom =xfld("t-origin", "From (airport code)", { value: "DEL", maxlength: "3" }), tStops =xfld("t-stops", "Stops and nights", { value: "BOM:3, GOI:2", placeholder: "BOM:3, GOI:2" });
    const tStart =xfld("t-start", "Earliest start", { type: "date" }), tFlex =xfld("t-flex", "Flexible by (days, 0 to 3)", { type: "number", min: "0", max: "3", value: "1" });
    const tFixed = el("label", { class: "fld" }, el("input", { id: "t-fixed", type: "checkbox" }), el("span", { text: " Keep my stop order" }));
    const tGo = el("button", { class: "btn primary", id: "t-go", type: "button", text: "Find the best trip", on: { click: () => {
      const stops = $("#t-stops").value.split(",").map((x) => x.trim()).filter(Boolean).map((x) => { const [code, n] = x.split(":"); return { code: (code || "").trim().toUpperCase(), nights: parseInt(n, 10) }; });
      run(tGo, "/api/travel/trip", { origin: $("#t-origin").value.trim().toUpperCase(), stops, earliest_start: $("#t-start").value, flex_days: xnum($("#t-flex").value) || 0, fixed_order: $("#t-fixed").checked }, renderTrip);
    } } });
    const wCity =xfld("w-city", "City", { value: "Pune" }), wDate =xfld("w-date", "Date", { type: "date" });
    const wGo = el("button", { class: "btn", id: "w-go", type: "button", text: "Weather", on: { click: () => run(wGo, `/api/weather?city=${encodeURIComponent($("#w-city").value)}&date=${encodeURIComponent($("#w-date").value)}`, null, renderWeatherDay, "GET") } });
    const card = (title, ...kids) => el("section", { class: "card" }, el("h2", { text: title }), ...kids);
    host.append(
      el("h1", { text: "Explore deals, places and trips" }),
      el("p", { class: "muted", text: "Advertised prices and fares are leads from public sources, not quotes. EventMate never books or pays." }),
      card("Ask in your own words", freeForm, status), msg, out,
      card("Best deals on the web", el("div", { class: "actions" }, dKind, dCity, dGuests, dNights, dBudget, dGo)),
      card("Best-rated places (Google)", el("div", { class: "actions" }, pKind, pCity, pRev, pGo)),
      card("Multi-city trip: the system tries the orders and dates", el("div", { class: "actions" }, tFrom, tStops, tStart, tFlex, tFixed, tGo)),
      card("Weather on the day", el("div", { class: "actions" }, wCity, wDate, wGo)));
  }
  function closeExplore() { state.exploring = false; $("#explore-view").hidden = true; render(); }

  function xlink(url, text) { return el("a", { href: url, target: "_blank", rel: "noopener noreferrer nofollow", text }); }
  const xrange = (lo, hi) => (lo === hi ? inr(lo) : `${inr(lo)} to ${inr(hi)}`);
  function renderDeals(d) {
    const root = el("div", { id: "deals-out" });
    root.append(el("p", { class: "muted", text: d.notice }));
    if (d.market) root.append(el("p", { id: "deals-market", text: `Market (${titleCase(d.market.unit)}): median ${xrange(d.market.median_low_inr, d.market.median_high_inr)} across ${d.market.n_offers} advertised prices on ${d.market.n_domains} sites.` }));
    if (!d.offers.length) root.append(el("p", { class: "muted", text: "No advertised prices could be read. Many sites load prices with JavaScript or block automated readers." }));
    for (const o of d.offers) root.append(el("div", { class: "alt deal" },
      el("b", { text: `#${o.rank} ${xrange(o.low_inr, o.high_inr)} ${titleCase(o.unit)}` }),
      o.est_total_low_inr != null ? el("span", { class: "tag " + (o.within_budget === false ? "rej" : "eli"), text: `about ${xrange(o.est_total_low_inr, o.est_total_high_inr)} for you${o.within_budget === true ? ", fits budget" : o.within_budget === false ? ", over budget" : ""}` }) : null,
      el("div", { class: "muted", text: `${o.vs_market_pct > 0 ? "+" : ""}${o.vs_market_pct}% vs the median; ${o.corroborated_by} other site(s) advertise a similar price` }),
      el("div", {}, xlink(o.url, o.domain), " ", el("span", { class: "muted", text: o.label })),
      ...o.flags.map((f) => el("div", { class: "notice warn", text: f })),
      state.planId ? el("button", { class: "btn small save-lead", type: "button", text: "Save as lead for this plan", on: { click: async (ev) => {
        const b = ev.target; b.disabled = true;
        const r = await api(`/api/plans/${encodeURIComponent(state.planId)}/leads`, { method: "POST", body: { kind: d.query.kind, url: o.url, domain: o.domain, label: o.label, unit: o.unit, low_inr: o.low_inr, high_inr: o.high_inr } });
        b.textContent = r.ok ? "Saved (see Budget tab)" : errText(r);
      } } }) : null));
    for (const w of d.warnings) root.append(el("div", { class: "notice warn", text: w }));
    root.append(el("p", { class: "muted", text: `Read ${d.pages_read} page(s) using ${d.provider}.${d.skipped.length ? " Skipped: " + d.skipped.map((x) => `${new URL(x.url).hostname} (${x.reason})`).join("; ") : ""}` }));
    return root;
  }
  function renderPlaces(d) {
    const root = el("div", { id: "places-out" }, el("p", { class: "muted", text: d.notice }));
    if (!d.places.length) root.append(el("p", { class: "muted", text: "No places found." }));
    for (const p of d.places) root.append(el("div", { class: "alt" },
      el("b", { text: p.name }), el("span", { class: "tag eli", text: p.rating != null ? `${p.rating} stars from ${p.rating_count || 0} reviews` : "no rating" }),
      p.price_level ? el("span", { class: "tag", text: "$".repeat(p.price_level) + " price band" }) : null,
      el("div", { class: "muted", text: p.address || "" }), p.maps_uri ? xlink(p.maps_uri, "Open in Google Maps") : null,
      ...p.reviews.map((r) => el("blockquote", { class: "muted", text: `${r.rating ? r.rating + "/5: " : ""}${r.text}` }))));
    root.append(el("p", { class: "muted", text: d.attribution }));
    return root;
  }
  function renderTrip(d) {
    const root = el("div", { id: "trip-out" }, el("p", { class: "muted", text: `${d.note} ${d.notice}` }));
    if (!d.packages.length) root.append(el("div", { class: "notice warn", text: "No complete itinerary could be priced." }));
    for (const p of d.packages) root.append(el("div", { class: "alt trip" },
      el("b", { text: `${titleCase(p.label)}: ${p.legs[0].origin} to ${p.order.join(" then ")} and back, from ${p.start_date}` }),
      el("span", { class: "tag eli", text: `${inr(p.total_price_inr)} · ${Math.floor(p.total_flight_minutes / 60)}h ${p.total_flight_minutes % 60}m in the air` }),
      el("div", { class: "muted", text: `${p.below_median_inr >= 0 ? inr(p.below_median_inr) + " under" : inr(-p.below_median_inr) + " over"} the median itinerary` }),
      ...p.legs.map((l) => el("div", { class: "muted" },
        `${l.date}: ${l.origin} to ${l.destination}, ${inr(l.option.price_inr)}, ${l.option.stops === 0 ? "non-stop" : l.option.stops + " stop(s)"}, ${l.option.segments[0].airline_name} ${l.option.segments[0].flight_number} `,
        l.option.booking_url ? xlink(l.option.booking_url, "view on Google Flights") : null))));
    for (const i of d.insights) root.append(el("div", { class: "notice", text: i }));
    for (const w of d.warnings) root.append(el("div", { class: "notice warn", text: w }));
    root.append(el("p", { class: "muted", text: `${d.searches_used} searches over ${d.orderings_considered} stop order(s).` }));
    return root;
  }
  function renderWeatherDay(w) {
    if (!w.slots.length) return el("p", { id: "weather-out", text: `${w.resolved_place}: ${w.note}` });
    return el("p", { id: "weather-out", text: `${w.resolved_place}: ${w.temp_min}°C to ${w.temp_max}°C, ${w.conditions || "no description"}; chance of rain up to ${Math.round(w.max_rain_chance * 100)}%. ${w.attribution}` });
  }
  function renderRouted(d) {
    const root = el("div", { id: "routed-out" }, el("p", { class: "muted", text: `Understood as: ${d.action}${d.action === "none" ? "" : " " + JSON.stringify(d.params)} (chosen by ${d.model}; checked by the same rules as the forms)` }));
    if (d.action === "none") { root.append(el("p", { text: d.note })); return root; }
    const view = { deals: renderDeals, places: renderPlaces, trip: renderTrip, weather: renderWeatherDay, flights: renderFlightsOut, discover: renderDiscover }[d.action];
    root.append(view(d.result));
    return root;
  }
  function renderFlightsOut(d) {
    const root = el("div", { id: "flights-out" }, el("p", { class: "muted", text: d.notice }));
    for (const o of d.options.slice(0, 10)) { const s = o.segments[0]; root.append(el("div", { class: "alt" }, el("b", { text: o.price_inr != null ? inr(o.price_inr) : "no price" }), ` ${s.airline_name} ${s.flight_number}, ${s.departs.slice(11, 16)} to ${o.segments[o.segments.length - 1].arrives.slice(11, 16)}, ${o.stops === 0 ? "non-stop" : o.stops + " stop(s)"} `, o.booking_url ? xlink(o.booking_url, "view on Google Flights") : null)); }
    if (!d.options.length) root.append(el("p", { class: "muted", text: "No flights found." }));
    return root;
  }
  function renderDiscover(d) {
    const root = el("div", { id: "discover-out" }, el("p", { class: "muted", text: d.notice }));
    for (const c of d.candidates) root.append(el("div", { class: "alt" }, el("b", { text: c.name }), el("div", { class: "muted", text: [c.street, c.city].filter(Boolean).join(", ") + (c.capacity_tagged ? ` · capacity ${c.capacity_tagged} (tagged, unverified)` : "") }), c.website ? xlink(c.website, c.website) : null));
    if (!d.candidates.length) root.append(el("p", { class: "muted", text: "No places found." }));
    root.append(el("p", { class: "muted", text: d.attribution }));
    return root;
  }
  $("#explore-open").addEventListener("click", () => (state.exploring ? closeExplore() : openExplore()));

  boot();
})();
