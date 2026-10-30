# EventMate decision log

Append-only. Never edit or delete an accepted entry; supersede it with a new one
(`Status: superseded by ADR-NNN`). Rule: `AI_CODING_RULES.md` → "Memory files".
Format per entry: Status · Context · Decision · Why · Rejected alternatives · Consequences.

Entry headings must be exactly `## ADR-NNN: title`, numbered consecutively (a test enforces this).

---

## ADR-001: Keep the name EventMate; do not rename to EventMind
Status: accepted (2026-10-30)
Context: A research note proposed "EventMind" as a project title.
Decision: Package, docs and UI stay `EventMate`. The pitch line may use the note's wording
("constraint-aware, explainable event copilot").
Why: A rename touches the package, CLI, docs, tests and logs and adds no capability.
Rejected: Rename to EventMind.
Consequences: None.

## ADR-002: Deterministic core stays; LLMs only propose, parse or phrase
Status: accepted (2026-10-30)
Context: Several proposed features (negotiation, extraction, outreach, browser research) invite an
LLM-agent swarm (8 agents in the research note).
Decision: The Manager, Planner, Validator, Replanner and every new policy check stay deterministic.
New LLM uses are limited to (a) proposing a structured parse or extraction that must pass a strict
Pydantic schema, and (b) wording. An LLM output is never a fact until a tool, the database or the user
confirms it (rule 1).
Why: The existing guarantees (independent validation, no invented facts) depend on it, and it is the
product's differentiator.
Rejected: LangGraph rewrite of the manager (rule: don't rewrite `src/`/pipeline without a tested need);
an LLM "Guardrail Agent" (guardrails must be code, not a model that can be talked out of them).
Consequences: "Agents" in new features are deterministic services with typed contracts.

## ADR-003: Budget rebalancing = Trade-off Negotiator that proposes, never applies
Status: accepted (2026-10-30)
Context: Category caps are hard filters; the repair message only says rebalancing would help.
Decision: Add a deterministic `tradeoff` module. When a plan fails on budget or a cap, it enumerates
candidate moves (downgrade tier, drop optional category, move date to cheaper day, shift cap between
categories, shorten hours), prices each from database records, ranks by
`savings * (1 - preference_harm)`, and returns the top N with savings, harm and risk. The user picks one;
applying it goes through the Replanner (new immutable version + diff). Caps stay hard filters.
Why: Auto-rebalancing would silently violate user caps (rule 5/6). Ranked, verified options are the novelty.
Rejected: Automatic rebalancing; LLM-suggested trade-offs (prices would be unverified).
Consequences: `preference_harm` weights must be explicit data in `EventPolicy`, not hidden constants.

## ADR-004: Scheduler production model = timezone-aware, calendar-aware, multi-resource
Status: accepted (2026-10-30)
Context: Scheduler assumes one organizer, local time, no working hours or time zones.
Decision: Use IANA zones via `zoneinfo`; store UTC instants plus the event's zone. Add per-vendor and
per-organizer availability windows and working hours as data; model organizers as a CP-SAT
`Cumulative` resource (capacity = organizer count). Multi-day events are a list of day segments.
Why: CP-SAT already models intervals and resources; this extends data, not the solver.
Rejected: Full calendar sync (Google Calendar) as the source of truth: it is an export/import target only.
Consequences: Schema change; old plan versions must still load (version the schema, add a migration test).

## ADR-005: Parser stays deterministic; add a locale layer and a confirm-the-parse step
Status: accepted (2026-10-30)
Context: Parser handles English and INR only.
Decision: Add rule-based locale packs (Indian numbering: lakh/crore/k, ₹, Devanagari digits, common
Hinglish tokens, Indian date formats). For free-form or other-language text an LLM may propose an
`EventRequest`, which is shown to the user as a parse-confirmation card and only used after the user
confirms. Ambiguity remains an error.
Why: Fail-closed (rule 6) holds; coverage grows without guessing.
Rejected: Silent LLM parsing; multilingual NLU model on the critical path.
Consequences: Adversarial tests per locale pack (mixed scripts, hostile text).

## ADR-006: Real vendor data via an observation ledger, confirmed in writing
Status: accepted (2026-10-30)
Context: "Confirmations are what the user says"; seed data is synthetic.
Decision: Introduce `Observation` records: `source_type` (seed, user_input, vendor_quote,
vendor_confirmation, public_listing), `value`, `observed_at`, `expires_at`, `confidence`, `evidence_ref`.
The Vendor agent reads only unexpired observations; an expired quote fails closed with a
"re-quote needed" repair. A booking counts as confirmed only after a `vendor_confirmation` observation
(vendor replied on the written channel, or used a one-time confirm link) or an explicit user override
that is labelled as such.
Why: Gives real data a provenance model without trusting calls or LLM extraction.
Rejected: Scraping directories into the vendor DB as facts; treating call transcripts as the record.
Consequences: Plans display "confirmed by vendor" vs "confirmed by you".

## ADR-007: Quote outreach order = written channel first (email/WhatsApp), voice last
Status: accepted (2026-10-30)
Context: Research note proposes AI voice negotiation.
Decision: Phase order: (1) structured quote-request via email and WhatsApp Business API to opted-in or
claimed vendors, with extraction into observations and written confirmation; (2) bounded text
negotiation; (3) voice pilot only after legal review.
Why: Durable record, validity window, vendors already use WhatsApp, lower legal exposure.
Rejected: Voice-first; cold outreach to vendors who have not opted in.
Consequences: Not verified here: current TRAI/DND/DLT rules, WhatsApp Business policies and pricing, DPDP
obligations. They are launch blockers in `ROADMAP.md`, to be checked by a human before phase 3 ships.

## ADR-008: Negotiation is bounded by user-set limits and honesty rules
Status: accepted (2026-10-30)
Context: Negotiation could mislead vendors.
Decision: A `NegotiationMandate` (ceiling, max rounds, non-negotiables, deadline) is data the engine
enforces in code. The agent identifies itself as an AI assistant acting for the customer, never invents
competing quotes or deadlines (it may cite only real observations it holds), and escalates to a human on
anger, ambiguity, legal terms or any payment-detail request. Accepting, depositing or signing always needs
human approval.
Rejected: Open-ended LLM negotiation; "pressure" tactics.
Consequences: Every message is logged with the mandate version.

## ADR-009: Credential Wallet = capability broker, not a password store
Status: accepted (2026-10-30)
Context: Agents will need Gmail/Calendar/Sheets access.
Decision: Build `wallet/`: OAuth refresh tokens encrypted at rest (AES-256-GCM with a key from OS keyring in
dev and a cloud KMS/secret manager in prod); `Grant` objects (who, service, scope, purpose, target, expiry,
uses); approval queue; short-lived capability handles; the secret is injected only at the API boundary;
append-only audit log; "revoke all". Least-privilege scopes (draft before send).
Why: Satisfies rule 7 and makes outbound actions auditable.
Rejected: Storing vendor-portal passwords (prefer magic links/OAuth; revisit only with explicit user
consent and a concrete need); storing card data (never; use a payment provider); storing browser
cookies/sessions (high risk, low value now); adopting third-party `agent-vault` code as a dependency before
it is cloned, read and vetted (ADR-013).
Consequences: Wording: say "the model never receives secrets", not "zero exposure" (the backend still
handles them).

## ADR-010: Browser research agent is read-only, sandboxed and data-only
Status: accepted (2026-10-30), scheduled late (phase 5)
Context: Proposal: browser agent that finds vendors, fills forms, sends enquiries.
Decision: Only read-only discovery, in an isolated profile with no credentials. Page text is untrusted data
(rule 8): it can only produce `public_listing` observations with low confidence, which a human promotes.
Form submission and sending always go through the wallet approval queue.
Rejected: Gemini Auto Browse / Chrome Enterprise as a dependency (enterprise-licensed, unverifiable
behaviour, not under our guardrails); autonomous form filling; scraping against site terms.
Consequences: Prompt-injection tests with hostile pages are mandatory.

## ADR-011: Gmail/Sheets/Calendar via direct OAuth APIs; Google MCP optional later
Status: accepted (2026-10-30)
Context: Options: Gemini in Workspace, Workspace MCP server, direct APIs.
Decision: Use direct Google APIs behind the wallet. The database, not a Sheet, is the source of truth;
Sheets and Calendar are user-facing exports.
Rejected: Gemini Workspace as the execution layer (not controllable by our validator); competitor tools
(DoMyWork, Planned, Simular) are benchmarks only, not dependencies.
Consequences: Fewer moving parts; MCP can be added behind the same wallet interface.

## ADR-012: Feature triage (accept / defer / reject), 2026-10-30
Status: accepted
See `ROADMAP.md` for the full table. Rejections and reasons are recorded there and are binding until
superseded by a new ADR.

## ADR-013: Reference repos are cloned into `_refs/`, read, and vetted before borrowing
Status: accepted (2026-10-30)
Context: The research note names GitHub repos (agent-vault, google-workspace-mcp, trip-planner, hitched,
etc.). Their existence, licences and quality were not verified in this session.
Decision: At the start of each phase, clone only that phase's references into `../_refs/`, record
licence and commit hash in `HANDOFF.md`, and reimplement patterns in our typed style. No vendored code
without a compatible licence. No runtime dependency on an unvetted repo.
Consequences: `_refs/` stays outside `eventmate/`.

## ADR-014: Production serving = single deployable with real auth, TLS at the proxy
Status: accepted (2026-10-30)
Context: Web server is local, single-user; remote use needs TLS, WSGI server and a token.
Decision: Gunicorn (or equivalent) behind Caddy/nginx for TLS; per-user auth via OIDC (Google sign-in
reuses the wallet's OAuth); users own their plans (tenant id in every record and path); SQLite → Postgres
for plans/observations/audit with append-only semantics and the same hash/HMAC checks; CSRF and rate
limits; secrets from env/secret manager; health endpoint. The static token remains for local/dev only.
Rejected: Exposing the dev server; multi-tenant file storage.
Consequences: Plan store gets a storage interface with file and SQL backends.

## ADR-015: Trade-off ranking is utility, caps are user-approved request data, validator recomputes allocation
Status: accepted (2026-10-30). Refines ADR-003 (does not supersede it).
Context: Implementing F1. ADR-003 ranked by `savings * (1 - harm)`, but a failing request has no baseline
plan, so "savings" is undefined. Also, a cap rebalance has to be applied somewhere the Validator can check.
Decision:
1. Options are ranked by `utility = (1 - preference_harm) - extra_spend / budget`. The savings formula
   returns only when a baseline plan exists (a future "cut cost to target X" feature).
2. A rebalance is stored as `EventRequest.cap_weights` (one positive weight per requested service, empty =
   policy default). `compute_allocation` is a pure function shared by the Budget agent and the Validator.
3. The Validator recomputes the whole allocation from the request and compares it with the draft
   (previously it only compared totals and trusted the draft's caps; that was a "validator trusts its input" gap).
4. Every option is verified by a full dry run (`ManagerAgent.run_request(persist=False)`), so nothing
   unplannable is ever shown. Moves in this version: rebalance caps, drop optional service, shift date to a
   database-backed date, minimum budget raise (binary search; uses re-split caps when that helps).
Why: Keeps "repairs must be verified" and "validator must not trust its input" true.
Rejected: LLM-suggested trade-offs (unverified prices); automatic application (ADR-003); tier/hours downgrades
(the database has no tier or hours data; add data first).
Consequences: `BudgetPolicy` must be the same object for the Budget agent and the Validator (the service
shares one). Dry runs write decision and tool-call log entries under option request ids (log noise, accepted).
Rebalancing cannot help when the cheapest eligible set exceeds budget minus contingency; the negotiator then
offers other moves instead of a fake rebalance.

## ADR-016: Budget Reality Check is a labelled estimate from the cheapest eligible vendor per service
Status: accepted (2026-10-30). Implements F2; the work was done in the previous session but was not logged (handoff was stale).
Context: Users should learn immediately whether a budget is realistic, before any plan exists, without the
system pretending to know market prices it does not have.
Decision:
1. `eventmate/pricing/reality.py::reality_check` is a pure function over database records. Floor = sum of the cheapest
   *eligible* vendor per requested service (category caps lifted, every other hard constraint applied) via the
   shared helper `cheapest_eligible` (also used by the Trade-off Negotiator; no duplicate logic).
2. Verdicts: `comfortable` (floor <= `BudgetPolicy.comfortable_bp` of budget after contingency, default 85%),
   `tight`, `contingency_breach` (fits only by spending the reserve), `unrealistic` (floor > budget), `unknown`
   (some service has no eligible vendor: no floor is computed and none is invented).
3. Every result carries `basis` ("Estimate, not a quote ... development seed data"), `source=tool:reality_check`,
   a per-category breakdown and a minimum budget that keeps the contingency intact (computed with integer arithmetic).
4. It is logged as a decision (confidence 0.7, or 0.3 when `unknown`), surfaced in the API (`reality`, also on 422),
   the UI, and the CLI `plan` command on **stderr** so stdout stays the plan itself.
Rejected: LLM or web-scraped "market rates" (unverifiable, violates rule 1); using the average price (not a
provable bound); refusing to plan when `unrealistic` (the user may still want the verified violations and trade-offs).
Consequences: While the data is synthetic the verdict is only as good as the seed data; the label says so. When real
quotes arrive (ADR-006) the floor should prefer unexpired observations and say which it used.

## ADR-017: Budget ledger is append-only, user-reported, and recomputed against the latest plan
Status: accepted (2026-10-30). Implements F3.
Context: Users need planned / quoted / paid / pending / variance per category that survives replans. The system
has no payment integration and must not gain one by accident.
Decision:
1. Storage: one append-only JSONL file per plan, `<plan_dir>/ledger/ledger-<plan_id>.jsonl`, written through the
   shared `JsonlLog` (0600, `O_NOFOLLOW`, validated model before write, fsync). Entries are `quote`, `payment`
   or `refund`, whole-INR `amount` in (0, MAX_BUDGET_INR], `note` <= 200 chars, `source` fixed to `user_input`.
2. Nothing is edited or deleted. A correction is a new entry (a newer quote replaces the older one; a refund
   offsets a payment). A refund larger than the amount paid is refused and not written.
3. The summary is recomputed on read from the file plus the *latest* plan version, so a replan never loses payment
   history. committed = latest quote, else planned cost; pending = max(0, committed - paid); `overpaid` is
   reported, never hidden; variance = committed - planned; headroom = budget - committed (negative = over budget).
4. New entries for a category that is not in the latest plan are refused. If a later replan drops a category that
   already has payments, the net paid amount is reported as `orphaned_paid` (shown as a warning in the UI) instead of
   vanishing from the summary; the entries stay in the file.
Rejected: editing/deleting entries (audit trail); storing in the immutable plan versions (every payment would
spawn a plan version and break "plan = validated artifact"); any payment initiation, bank or UPI integration
(out of scope, regulated, and contradicts the "records, never pays" rule); floats for money.
Consequences: The ledger file is not covered by the plan store's SHA/HMAC (no `EVENTMATE_SIGNING_KEY` check).
Anyone who can write the plan directory can alter it. Acceptable for local use; the SQL store (ADR-014) should
add per-row integrity. The ledger is not yet an input to the Validator or to replanning.

## ADR-018: User answers on open questions (2026-10-30): no managed Postgres, vendor city/language unspecified, user owns legal checks
Status: accepted (2026-10-30). Refines ADR-007 and ADR-014 (supersedes neither).
Context: The user answered the handoff's open questions: the first vendor city and languages are not specified, no managed
Postgres is available, and the user will check the legal/policy blockers themselves.
Decision:
1. Production storage starts on **SQLite** (WAL mode) behind the storage interface from ADR-014; Postgres stays a later
   backend, not a prerequisite. One app process owns the database file; if that stops being true, move to Postgres.
2. With no named language or city, voice (F21) stays **Deferred** and nothing in Phase 2-4 may depend on one language.
   Locale work (F6) therefore targets English plus Hinglish/Devanagari digits as data-driven packs, and the vendor seed data stays multi-city.
3. Outbound messaging (F11), recording and voice remain **blocked** until the user reports the legal checks done; the
   wallet/approval queue (F12/F13) may be built first because it sends nothing by itself.
Rejected: Starting on Postgres (no host, extra ops burden); picking a default vendor language to unblock voice (invented requirement).
Consequences: SQLite limits to a single writer process; the SQL store must keep the append-only and hash/HMAC rules.

## ADR-019: Locale pack v1: lakh letter, Indic digits (allow-list), day-first numeric dates; ambiguity stays an error
Status: accepted (2026-10-30). Implements the deterministic part of ADR-005 (F6, partial).
Context: Probing the parser found a silent wrong answer, `₹ 2L` parsed as INR 2, plus no support for Indic digits
or numeric dates (`12/12/2026`).
Decision:
1. A bare `L`/`l` after a number is lakh (also `Cr`, `k`, `lac`, `lakh` as before).
2. Digits of Indian scripts (Devanagari, Bengali, Gurmukhi, Gujarati, Oriya, Tamil, Telugu, Kannada, Malayalam) are read as
   their decimal values; an assumption is recorded. A single number that mixes digit scripts is a parse error. Arabic-Indic
   and other non-ASCII digits still fail closed (the earlier red-team test `test_non_ascii_digits_fail_closed` stays valid).
3. Numeric dates `DD/MM/YYYY`, `DD-MM-YYYY`, `DD.MM.YYYY` need a 4-digit year and the same separator twice. They are read
   day-first only when that is unambiguous (day > 12, or day == month); otherwise "ambiguous date" is an error that asks for
   `2026-12-05` or `5 December 2026`. This follows ADR-005 ("ambiguity remains an error") rather than assuming Indian
   day-first convention.
Rejected: Treating every non-ASCII digit as valid (unreviewed scripts, spoofing surface); defaulting ambiguous dates to day-first
(a wrong event date is costly and silent); 2-digit years.
Not built (still F6 open): Hinglish tokens, the LLM-proposed parse plus user confirmation card (LLM parsing is off by default
and no language was specified by the user, ADR-018), currencies other than INR.
Consequences: 20 new parser tests including hostile-digit input; the `BASE` name clash in `test_manager.py` taught us to
prefix new module-level test constants.

## ADR-020: F5 split: organizer capacity now, time zones and working hours later
Status: accepted (2026-10-30). Implements part of ADR-004; the rest of ADR-004 stays accepted and open.
Context: ADR-004 bundles three changes (multi-organizer, working hours/availability windows, IANA zones). Only the first
changes solver behaviour and can be verified now; the others need data and a UI that do not exist yet.
Decision:
1. `EventRequest.organizers` (int, 1..10, default 1) is the capacity of the scheduler's `organizer` resource. Parsed from
   "N organizers/organisers/coordinators/planners"; conflicting counts or values outside 1..10 are parse errors. Old stored
   requests and plans load with the default (tested).
2. The Validator's `spec_matches_policy` now expects organizer capacity == `request.organizers` and capacity 1 for every other
   resource, still computed from the request, never trusted from the draft (tamper test kept).
3. Replans keep the count automatically (`with_changes`).
Measured effect on the example (objective, lower is better): 1 organizer 57375, 2 organizers 57300, 3 organizers 57285; bookings
overlap from 2 organizers up. The gain is small because pre-event tasks are short; do not market it as a large speed-up.
Deferred (still ADR-004): IANA zone per event (all times stay naive local time), vendor/organizer working hours, multi-day events.
Rejected: A "team size" free-text field (not machine-checkable); making zones a display-only string before conversion logic exists
(it would look supported but do nothing).
Consequences: no UI control yet; organizers are set through request text (CLI/API/UI text box).

## ADR-021: F7 v1 = advisory dietary checklists in strict, versioned, review-labelled data packs
Status: accepted (2026-10-30). Implements the safe part of F7; refines ROADMAP F7 ("3 traditions") without superseding it.
Context: F7 asked for cultural/dietary rule packs. EventMate has no verified knowledge of rituals or traditions, and rule 1
forbids invented facts. Dietary requirements already work as hard constraints against caterer data.
Decision:
1. Packs are data (`eventmate/data/rule_packs.json`, schema_version 1), loaded by `eventmate/planning/rules.py` with the same fail-closed
   posture as the vendor data: size cap, `extra=forbid`, unique ids, plain text only (no `<`, `>`, newlines), item/length bounds.
2. A pack is keyed by a dietary requirement (jain, halal, vegan, vegetarian, gluten_free) and holds checklist items phrased as
   questions to ask a vendor or host ("confirm with the host family"). Items never pass or fail a constraint and never touch a plan.
3. Every pack carries `version`, `source`, `reviewed` and `reviewed_by`; a pack may only claim `reviewed: true` if it names a
   reviewer. All shipped packs are `reviewed: false` and the UI says "unreviewed draft". A test pins that none is reviewed, so
   flipping the flag is a visible, deliberate change.
4. Exposed as API `GET /api/plans/<id>/advisories`, `EventMate.advisories`, and an Overview card ("Things to confirm").
Rejected: shipping ritual, schedule or menu claims for specific traditions (unverifiable by me; wrong content about someone's
tradition is worse than none); LLM-generated cultural text (rule 1); making advisories hard constraints (not checkable against data);
accessibility packs (the venue data has no accessibility field; add data first).
Consequences: Tradition packs (rituals, timing, invitation wording) remain open and need named reviewers from those communities;
that is a user action, recorded in HANDOFF open questions. Pack text is English only until the locale work extends to output.

## ADR-022: Conversational intake: the AI proposes, the human approves, and the model has no authority
Status: accepted (2026-10-30). Implements the "LLM-proposed parse plus confirmation card" part of ADR-005 (F6) and keeps ADR-002.
Context: The user dislikes long forms of data boxes and wants an AI to collect event details, with a human in the loop wherever
it matters and no prompt-injection path. OpenMuse (`_refs/openmuse`, MIT, commit a4944f4) was read as a reference: its clarification
panels are strict-schema choices the model can only pick between, its approvals are bound to the reviewed content, and its own
SECURITY.md admits an injected agent with open egress can leak data. We copy the ideas, not code.
Decision:
1. `eventmate/planning/intake.py` turns the organiser's words into an `IntakeDraft` (every field optional). The deterministic parser always
   runs; if `EVENTMATE_ENABLE_LLM=1` and a key exists, an LLM also reads the message (`prompts/intake.extract.v1.md`, output format only).
2. The model has no tools, no network, no state and no authority. Its reply is accepted only as one JSON object that passes the
   strict schema (`extra=forbid`, enums, bounds, short plain-text city of at most 3 words). Anything else is dropped and the
   deterministic reading is used, with a note to the user.
3. Defence against prompt injection, in layers: user text is data inside fenced blocks and the fence tokens are stripped from it;
   the client-supplied draft is schema-validated before it is embedded; a city the user did not write is dropped; every value the AI
   gives that the parser cannot corroborate is flagged amber on the card; the output is never rendered as HTML or run as an instruction.
4. Human in the loop: nothing is planned until the user presses confirm on a one-sentence review card where every value is editable.
   Two different values for one fact (guest count, date, budget) become one-tap choices, and an ambiguity clears any earlier or
   AI-picked value. Proposing stores nothing. The approved plan carries a receipt (`draft sha256:<12 hex>`) in its assumptions.
5. Questions come from code templates, not from model text. Missing required facts (guests, city, date, budget) block confirmation.
6. What the LLM receives: the reviewed draft as JSON, today's date and the newest message. Nothing else (no plans, vendors, guest lists).
Rejected: letting the model call tools or build the plan directly; free-text model questions (an injection surface); auto-confirming
when the AI is confident; trusting a model-supplied city because it is a known city; adding more form fields.
Not done: new dietary values (sattvic, allergens) and accessibility facts are not in the schema yet, so the AI cannot collect them;
the classic one-block form stays under "Write it as one block instead" and all old paths still work; no multi-user storage of drafts.
Consequences: 49 tests in `tests/test_intake.py` (injection, malformed model output, ambiguity, receipt, web) plus a Chromium test.
Residual risk, stated plainly: a hostile message can still push a wrong but well-formed value (for example a different budget) into
the draft. The flag, the visible card and the explicit confirm are the control; there is no automated defence beyond them.

## ADR-023: The assistant: grounded, permissioned, cached, traced and change-controlled, with a model that has no authority
Status: accepted (2026-10-30). Extends ADR-002 and ADR-022. Reuses the approval-queue idea of ADR-009 / F13 for assistant-suggested changes.
Context: The user asked for a production-grade assistant covering: a system prompt, RAG over our own documents, tool use, role-based
access carried into every tool, guardrails against prompt injection and PII leaks, a cache and a model router for cost, traces and
evals, and change control so a prompt cannot be tweaked on a Friday without evals. References read (ideas only, nothing copied or
installed): openmuse (MIT, a4944f4), guardrails-ai (Apache-2.0, 06d0ff2), portkey gateway (MIT, 669825c), haystack (Apache-2.0, 4b5dd70),
langfuse (MIT core with ClickHouse "ee" parts, 5cb2047), dify (modified Apache with extra conditions, f36f75a4: ideas only).
Decision (code in `eventmate/assistant/`):
1. The model is untrusted. Tools are typed; a call needs the asker's permission (`identity.py`, three roles), a strict argument schema,
   and runs on the plan fixed by the session. Forbidden tools are not shown to the model, and unknown and forbidden answer identically.
2. A mutating tool never acts. It files a `Proposal` bound to the plan version, tool and exact arguments (content hash). Only a human
   with the approval permission, on a separate CSRF-protected route, applies it, only while the plan is unchanged. Capped, de-duplicated, logged.
3. RAG: BM25 over chunks from Markdown docs, the live `BudgetPolicy` (numbers generated from code) and the rule packs. Permission
   filtering happens before ranking. Answers must cite ids from this conversation; the output guard rejects numbers, links, markup and
   names the evidence did not supply, and applies a data-owned deny list (`data/assistant_policy.json`, competitors and phrases).
   On failure: one re-ask with the reason (guardrails "reask"), then a deterministic fallback of the evidence.
4. Data is fenced and PII-masked on the way in; fence tokens in data are stripped; an injection scan marks hostile data in the trace and
   keeps it out of the cache. It is a signal, not the defence: the defence is points 1-3.
5. Cost: a code router sends routine questions to templates with no model, short ones to a small model, hard or change-making ones to a
   large model. Answers are cached with single-flight (identical in-flight questions run once). The key holds roles, plan and ledger state,
   knowledge version and prompt hash, so nothing crosses permissions or goes stale. Retries with backoff and fallback to the other tier
   (portkey pattern); per-user hourly estimated-token cap that drops to the no-model path.
6. Observability: one JSONL trace per question (route, cache, steps, tools with masked arguments, models, estimated tokens, prompt and
   knowledge hashes, outcome); the raw question is stored only as a hash. The UI shows it under "How I answered" to the organiser.
7. Change control: `prompts/assistant.lock.json` pins the prompt hash. A test fails if the prompt changes without
   `python -m eventmate.assistant.evals --update-lock`, which refuses unless the static evals pass (required safety clauses present) and
   retrieval recall is 1.0, and, with `--behavioral` and a model, the model cases pass. The lock records honestly whether the behavioral
   cases ran.
Rejected: native tool-calling or MCP servers for now (the JSON protocol works with any provider and keeps every call in one validated
path); a vector database or embeddings now (BM25 gets recall 1.0 on our labelled set; add embeddings when the corpus outgrows it); letting
the model hold identity or choose the plan; trusting an injection detector as a gate; storing raw prompts by default.
Not done, stated plainly: the behavioral evals have **never run against a real model** (none configured), so the lock says `not_run`;
the prompt's effect on a real model is unmeasured. Roles come from a server-side principal (default: local organiser); there is no login
or per-plan tenant yet (F23). The cache and token budget are per process and in memory. Estimated tokens are chars/4, not billed usage.
The proper-noun check can reject a good answer (safe failure) and cannot catch every outside recommendation: the deny list and the
behavioral evals are the other layers. Trace scores, prompt labels (langfuse) and curated reply annotations (dify) are later ideas.
Consequences: 79 tests in `tests/test_assistant.py` run against scripted models that obey attackers; a Chromium test covers the Ask tab.

## ADR-024: Langfuse and Dify patterns added to the assistant, at the user's direction
Status: accepted (2026-10-30). Amends ADR-023, which had marked langfuse and dify "ideas only" because of their licences.
Context: The user told us to use both and said they will handle the licences. That decision is theirs and is recorded here. In practice we
reimplemented the patterns in our own style and copied no source from either repo (the stacks differ), so no licence text was
carried into this codebase. The licence question for any future copying stays with the user.
Decision:
1. Langfuse patterns (`assistant/langfuse.py`, `trace.py`): optional export of each trace to a Langfuse server over its public
   ingestion API (trace, one span or generation per step, sessions keyed by plan, tags for route and outcome); thumbs feedback stored
   locally and sent as a score. Off unless LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are all set (environment only);
   non-local hosts must be https; it runs on a background queue that sheds load when full and can never delay or break an answer.
   What leaves the machine is what the local trace holds: PII-masked question and answer text, hashes, step metadata.
2. Dify patterns (`assistant/annotations.py`, `guard.py`, `knowledge.py`, UI): approved answers (the annotation reply): an approver turns a real
   answer into a reusable one, optionally rewording it; it matches similar questions (word-set similarity of at least 0.8) with no model, only for
   the same roles, only while the plan and ledger are in the state it was approved for. Input moderation with a preset reply from the
   policy file; a retrieval score threshold; suggested-question chips by role.
Rejected: sending raw (unmasked) text to Langfuse; making export on by default; trusting a client-supplied role or question when saving an approved answer
(the server reads both from the stored trace); serving an approved answer across roles or after the plan changed.
Not done, stated plainly: the Langfuse mapping was built from its ingestion schema in the cloned repo and tested against a fake endpoint;
it has **not** been sent to a real Langfuse server. Approved answers are matched by word overlap, not embeddings, so paraphrases can miss.
Token usage in exported generations is the estimate (chars / 4). Annotation storage is a local JSON file, single process.
Consequences: 15 more tests in `tests/test_assistant.py` and a longer Chromium Ask-tab test.


## ADR-025: Observation ledger v1: user-entered quote observations and deterministic quote intelligence, not yet a Validator input
Status: accepted (2026-10-30). Implements the first slice of F9 / ADR-006.
Context: ADR-006 wants real vendor data with provenance. Prices reach the plan through `cost_for` in six places (`evaluate`, Validator,
Replanner, reality check, trade-off, assistant tools). Rewiring all of them in one change would be a large unreviewable diff.
Decision:
1. Storage: one append-only JSONL file `<plan_dir>/observations/observation-all.jsonl` through the shared `JsonlLog` (0600, `O_NOFOLLOW`,
   model validated before write). Nothing is edited; a newer quote for the same vendor supersedes the older one on read and the history stays.
2. `QuoteObservation` fields: `vendor_id` and `category` (must match a record in the vendor repository, else refused), `source_type`
   (`user_input`, `vendor_quote`, `vendor_confirmation`, `public_listing`), `entered_by`, `observed_at`, **required** `expires_at`, `confidence`
   in [0, 1], `evidence_ref`, whole-INR `amount_inr` with `basis` total or per_plate (per_plate needs `guest_count`), `inclusions`,
   `exclusions`, `hidden_costs` (label plus optional amount). `vendor_quote` and `vendor_confirmation` require an `evidence_ref`
   (e.g. the email subject or WhatsApp message id); a quote claiming the vendor said it with no reference is refused.
3. Quote intelligence is code: effective total = quoted total + hidden costs that have an amount; hidden costs without an amount are listed
   as "unquantified" and never guessed; expected items per category come from `data/quote_checklists.json` (marked `reviewed: false`) and
   are reported as missing (not mentioned) or excluded (vendor says not included). Status is valid, expiring (3 days or fewer) or expired.
4. Comparison ranks unexpired quotes by effective total (ties by vendor id). Expired quotes are not ranked; each is listed with the
   repair "re-quote needed".
5. Text fields are data: control characters are refused, lengths capped, and nothing in them is ever executed, interpolated into a
   prompt or used as a path.
Rejected: LLM extraction of quotes from pasted emails in this slice (needs its own adversarial tests); treating `public_listing` as a
confirmed price; defaulting `expires_at`.
Not done, stated plainly: observations do **not** yet feed the Vendor agent, Validator, Replanner or reality check, so a plan still prices
from seed data and the "confirmed by vendor" label is not yet shown. That wiring is the next F9 step and must change all six `cost_for` sites
together with tests. The file is not covered by the plan store's SHA/HMAC. The checklists are unreviewed drafts.
Consequences: users can record and compare real quotes with their validity window and see hidden costs and missing items; no plan changes.

## ADR-026: Recorded quotes set prices; the repository is the single price resolver; an expired quote fails closed
Status: accepted (2026-10-30). Implements the pricing half of F9 / ADR-006; amends ADR-025 ("not yet a Validator input").
Context: Prices were read through `cost_for` in six places, so wiring quotes into only some of them would let the Vendor agent and
Validator disagree.
Decision:
1. `VendorRepository.price(provider, guests)` is the only price path (Vendor agent, `evaluate`, Validator, Replanner, reality check,
   trade-off). With no quote it equals `cost_for`. With a valid quote it is the quote (per_plate x current guests, or the total) plus the hidden
   costs that have an amount. Unpriced hidden costs are not guessed.
2. The latest non-`public_listing` quote counts; a public listing never sets a price. The latest quote wins even if an older one is still valid.
3. If the latest quote has expired, the vendor fails a new `quote_valid` check with "re-quote needed" and the repair says to ask for and record a
   fresh quote. A confirmed booking whose quote expired surfaces as `confirmed_booking_conflict` rather than being repriced silently.
4. Expiry is judged against the caller's `today` through `repo.clock`, set by `EventMate.plan/replan/tradeoffs/confirm_intake/apply_tradeoff`.
5. A per_plate quote is accepted only for a caterer.
Rejected: a second price path inside the Validator; falling back to the seed price when a quote expired (stale or synthetic money shown as current).
Not done: the "confirmed by vendor" vs "confirmed by you" label and `vendor_confirmation` handling; a Quotes UI card; the clock is a
mutable attribute set per call (single-user local server; revisit with ADR-014 multi-user); a quote recorded after a plan was made makes
`revalidate` fail on `cost_matches_database` until the user replans (tested; deliberate, but there is no one-click repair yet).
Consequences: 10 tests in `tests/test_quote_pricing.py`.

## ADR-027: Quote status per selection, "confirmed by vendor" is computed on read, re-pricing is a same-budget replan
Status: accepted (2026-10-30). Completes F9 part 3 (ADR-006, ADR-025, ADR-026).
Decision:
1. `EventMate.quote_status(plan_id, today)` returns one `SelectionQuoteStatus` per selection: plan cost, current price (from `repo.price`),
   `price_stale` (they differ), the quote report as given, and `confirmed_by`. API `GET /api/plans/<id>/quotes`; Budget tab "Vendor quotes" card.
2. `confirmed_by` is `you` when the user confirmed the booking, upgraded to `vendor` only if a `vendor_confirmation` observation (which always has an
   `evidence_ref`) exists for that vendor. A vendor confirmation never confirms a booking by itself: only the user does (ADR-006, provenance check).
   It is computed on read, so the immutable plan versions are untouched. It ignores `expires_at`, since a booking confirmation does not lapse.
3. Repair for a stale plan: the UI button sends a `budget_change` replan with the unchanged budget, which runs the full pipeline and writes a new
   version. No new endpoint and no new pricing path.
4. The quote report shows the quote as given (e.g. 100 plates); `current_price` is for this plan's guest count. The UI shows both.
Not done: confirmation identity is a free-text name, not a login; the evidence reference is not verified against a real message; no outbound
messaging (ADR-018 blocks it). The form clears after each save (found by the end-to-end test).

## ADR-028: External data sources for the MVP: fli for flights (done); Overpass for venues (next); paid or keyed sources deferred
Status: accepted (2026-10-30). Reverses the "Flights ... out of scope" rejection in ROADMAP at the user's direction; refines ADR-006 and ADR-013.
Context: The user asked for fli (Google Flights) to supply fares and schedules for travel booking, and listed other sources as reliable MVP data:
Foursquare Places MCP and OS Places, Overpass clients (aio-overpass and others), Duffel, Amadeus, Kiwi. They also reported that fli "fails at multi-city".
Findings: fli (MIT, commit 881aee5, read and run 2026-10-30) does not have a bug here. Google serves multi-city results through an RPC that needs a
header only the page's JavaScript can produce, so fli raises `SearchUnsupportedError` by design (upstream issue #223). One-way search works live
(DEL-BOM returned 61 results). Prices default to USD, so INR must be requested.
Decision:
1. Flights: `eventmate/integrations/travel.py`, `FlightProvider` interface, `FliProvider` adapter (fli imported lazily; optional dependency, missing = clear
   `TravelError`). Results are frozen models tagged `tool:fli`: INR whole rupees, schedule per segment, Google Flights deep link (https prefix enforced).
   A price in any other currency is never shown as rupees. Airport codes are validated (three capitals, must exist in fli's list) before any call, in the
   service, so every provider gets the same checks. API `/api/travel/flights`, `/api/travel/multi-city`; CLI `flights`, `multicity`.
2. Multi-city is composed from separate one-way searches, in order, dates non-decreasing, at most 6 legs. The result says it is not a multi-city fare.
   A leg that fails is reported on that leg and removes the total; nothing is estimated. We did **not** try to forge Google's gated header: that is a
   bot-protection token, and bypassing it is not ours to do.
3. Fares are indicative, never booked or paid by EventMate (AI_CODING_RULES: records, never pays). Flight data does not enter plans or the Validator yet.
4. Venue discovery (next slice): Overpass/OSM first (free, no key). Results are `public_listing` candidates with source `tool:overpass`; they are never
   written into the vendor database as facts (ADR-006 rejected that) and carry no price or availability.
5. Deferred, with reasons: Foursquare Places MCP needs an API key and its terms are unread; Foursquare OS Places is a very large dataset (a city slice
   would need a download and a licence read, which the user lists as Apache-2.0 but we have not verified); Duffel and Amadeus need accounts and create real
   orders or payments, so they stay behind the F12/F13 approval queue and are not touched in the MVP; Kiwi access terms are unverified.
Risks stated plainly: fli reverse-engineers an undocumented Google page, so it can break or be rate-limited without notice, and its terms of use are the
user's to check (same stance as ADR-024). Results are 20-60 rows and thinner for children or premium cabins.
Venue discovery as built: `eventmate/integrations/discovery.py` calls the Overpass API directly (the clients the user listed are thin wrappers, and an own query
builder keeps user text out of the query: the city name is allow-listed and quoted). API `/api/discover/venues`, CLI `discover`. Candidates are tagged
`tool:overpass`, carry the ODbL attribution, have no price or availability, and drop any optional field (website, capacity) that fails validation.
Results are cached for an hour; a busy public server (HTTP 429/502/503/504) is reported as busy. **Not verified live**: on 2026-10-30 overpass-api.de
answered "at capacity" and two mirrors timed out, so only fake-server tests ran. That server's notice says automated use belongs on a local or mirror
instance; set `EVENTMATE_OVERPASS_URL` to one for anything beyond light use. Discovery holds no app-wide lock while it waits on the network.
Consequences: 21 tests in `tests/test_travel.py`, 40 in `tests/test_discovery.py`, 4 CLI tests, one live flights run recorded in the handoff.

## ADR-029: Weather from OpenWeather; secrets come from a git-ignored `.env` through an allow-list loader
Status: accepted (2026-10-30). Implements the "weather only from a real tool" note on F18.
Context: The user asked for the OpenWeather API and for all keys to live in `.env`. They created `MultiAgentPlanning/.env` containing one bare 32-character
value with no variable name.
Decision:
1. `eventmate/env.py` loads `.env` (path: `EVENTMATE_ENV_FILE`, else `MultiAgentPlanning/.env`) into the process environment. Only names on a fixed
   allow-list are read; values already in the real environment win; the loader returns names and warnings, never values. It refuses a symlink, a
   non-regular file or one over 64 KB, and warns when the file is readable by others. AI_CODING_RULES still holds: the application reads
   environment variables; `.env` is just how they get set locally. `.env` is git-ignored; `.env.example` (no values) is the template.
2. The existing `.env` was reformatted to `OPENWEATHER_API_KEY=<value>`, mode 600. We assumed the bare value is the OpenWeather key, which is what the
   user said it was for; a 32-hex string matches that format but we could not confirm it.
3. `eventmate/integrations/weather.py`: free 5-day / 3-hour forecast plus geocoding. The key is sent only in the request URL and is never logged or returned: every
   failure is replaced by a fixed message (401, 404, 429, timeout, bad JSON, oversize). The city passes the same allow-list as discovery and is URL-encoded.
   Slots are grouped by the place's local date. Past the horizon, or before the forecast starts, the result has no slots and a note: nothing is estimated.
   Rain is "possible" at a chance of 0.3 or more (a code constant, not an LLM judgment). Place name resolved by OpenWeather is shown so a wrong
   match (two cities with one name) is visible.
4. API `/api/weather`, `/api/weather/status`, `/api/plans/<id>/weather`; CLI `weather`; Overview card. "Not configured" is HTTP 503 on the data
   endpoints and a plain `configured: false` on the status endpoint, which the UI asks first (a 503 from the card broke every browser test by logging a console error).
Not done: One Call 3.0 (needs a paid-card subscription), historical or climate averages for dates beyond five days; weather does not feed the planner or
the Validator. Not verified live: the first real call returned 401, probably because new OpenWeather keys take up to a couple of hours to activate, but
we cannot rule out a wrong value.
Consequences: 32 tests in `tests/test_weather.py`, 2 CLI tests, an e2e step. The test suite sets `EVENTMATE_ENV_FILE` to a missing path so a real `.env` never reaches tests.

## ADR-030: AI-first trip search = an LLM at the edges, a deterministic planner in the middle, tools for every fact
Status: accepted (2026-10-30). Builds on ADR-028; answers the user's request that travellers not check each multi-city hop by hand.
Context: The user wants an agent with internet access that takes requirements, runs its own logic, compares prices and deals, and returns the best packages,
not a chatbot. Our rules say facts come from tools and a plan is shown only after validation.
Decision:
1. Architecture: (a) intake: an LLM (already built, ADR-022) may turn free text into a structured `TripRequest`, which the user confirms; (b) search: plain code
   (`tripsearch.py`) generates stop orders (all permutations, up to 4 stops, or the given order) and start dates (`flex_days`), collects the *distinct* legs,
   searches each once in parallel, and builds complete itineraries; (c) ranking: three labelled packages (cheapest, fastest, best value) plus up to four
   alternatives, with the gap to the median itinerary and plain-language insights ("GOI then BOM is INR 627 cheaper"); (d) an LLM may phrase an explanation
   later, but never supplies a price, a date or a ranking. The ranking is code so every figure can be re-derived from the legs.
2. Budget and honesty: every trip states how many searches it needs *before* spending any; over the limit (default 30, hard cap 60) it is refused with a repair
   (fixed_order, fewer stops, less flex). A failed or unpriced leg drops only the itineraries that need it and is named in the warnings; if nothing is complete
   the result is empty with a reason. A deadline returns partial results and says so. Every search is written to the tool-call log.
3. "Best value" uses `value_per_hour_inr` (default 300), an assumption the user can change; it is labelled as such and is not data.
4. Still separate one-way searches, not a multi-city fare (ADR-028); the result carries that note.
Rejected: letting an LLM choose routes or prices; searching every date and order without a budget; hiding dropped itineraries.
Not done: hotels and ground transport between stops, visa and layover-time rules, price-drop tracking, a UI form, the LLM intake for trips, and packaging flights
with venue/stay costs. Flight data does not yet enter event plans.
Live check 2026-10-30: DEL to BOM and GOI, 2 stops, flex 1: 12 searches in 4 seconds; cheapest GOI then BOM INR 19,897 against INR 20,524 for the order tried by hand.
Consequences: 28 tests in `tests/test_tripsearch.py`.

## ADR-031: A read-only web deals agent: search, read pages safely, extract advertised prices in code, rank with caution
Status: accepted (2026-10-30). Implements the user's request for an agent with internet access that finds several deals online and lists the best of the best,
when no reliable pricing API exists. Revisits ADR-010 (browser discovery was deferred): this is plain page reading, with no JavaScript, logins or cookies.
Decision:
1. Layers: `web_access.py` (SafeFetcher), `websearch.py` (providers), `deals.py` (agent), `data/deal_rules.json` (query templates, units, bounds, keywords; unreviewed).
2. SafeFetcher treats the web as hostile: http(s) only, default ports, no credentials, every resolved address must be public (loopback, private, link-local, metadata,
   IPv6 mapped and numeric-host tricks are refused), redirects re-checked up to 3 hops, the connection is pinned to the checked IP (TLS verified against the real
   host), robots.txt obeyed (404 = allowed, unreadable = disallowed), 1.5 MB and 15 s caps, identity encoding only, no cookies, only text/html and text/plain, scripts,
   styles and comments dropped, control characters stripped, text capped at 200k characters. Honest User-Agent `EventMateBot`.
3. Search: Brave Search API when `BRAVE_SEARCH_API_KEY` is set (official, untested live: no key), otherwise DuckDuckGo's HTML endpoint, which is unofficial, can be rate-limited
   or change, and whose terms are the user's to check (same stance as ADR-024 and ADR-028). Only the city name from the user reaches a query, after the allow-list.
4. Extraction is regular expressions over page text (rupee symbol, Rs, INR; lakh, crore, k; ranges; per plate, head, day, hour, event). No LLM is used, so a price cannot be invented and
   a page cannot instruct the agent. A mention needs a currency marker, a sane range, bounds per service and a keyword for the service; "per head" is treated as "per plate" for catering;
   a missing unit is assumed only for venues and photographers and flagged.
5. Ranking in code: one unit at a time (the most common), market median, percent against it, corroboration (other sites with a similar entry price within 25 percent), at most 3 offers
   per site, estimated total for the guest count, budget fit. Offers far below the market (-50 percent or worse) that no other site corroborates are flagged "unusually low" and ranked after
   the solid ones, because live runs showed the cheapest rows are often add-ons or partial packages. Wide ranges are flagged.
6. Output is labelled "advertised prices ... not a quote". It never sets a plan price (ADR-025: public listings do not price) and is not written to the vendor database. Skipped pages and the reason
   are listed, and every search and fetch goes to the tool-call log. Budgets: 3 queries, 8 pages, 2 pages per site, 60 s.
Live runs 2026-10-30 (Pune): caterer 7 pages, 8 to 12 s, 20+ prices across 6 sites, market median about INR 650 to 1,150 per plate; photographer plausible INR 20,000 to 40,000 with
corroboration; **venue found prices on only one site** because most directories answered 403/503, were too large, or load prices with JavaScript.
Not done: JavaScript-rendered pages and sites that block automated readers (would need a real browser, which is ADR-010's deferred work with its legal review), PDF price lists, Google Places
ratings and reviews (needs the user's key; the Places adapter is next), a UI tab, an LLM that turns free text into a deals query, and any recording of a deal as a quote observation.
Known weaknesses: labels are cut from the surrounding words and can read oddly; a "starting at" price says little about what is included; sites can show different currencies or cities
on one page. Consequences: 28 tests in `tests/test_deals.py`, 43 in `tests/test_web_access.py`, one CLI test.

## ADR-032: Models come from NVIDIA (heavy) and OpenRouter (light and backup); Anthropic is removed; credits are guarded
Status: accepted (2026-10-30). Replaces the Anthropic client in ADR-022/023 and the "optional LLM polish" setup; the guard rails on model output are unchanged.
Context: The user is on free tiers with about five dollars of credit, supplies `NVIDIA_API_KEY` and `OPENROUTER_API_KEY`, wants NVIDIA for the high-complexity work, OpenRouter
routed models where they fit, no Anthropic, and no credits wasted on tests.
Decision:
1. `llm.py` gains `OpenAICompatClient` (the standard library against the OpenAI chat-completions protocol that both providers speak), `FallbackClient`, `UsageMeter` and a new
   `client_from_env(tier)`. Heavy tier: NVIDIA first, OpenRouter's free heavy model behind it. Light tier: OpenRouter's free light model first, NVIDIA behind. Either key alone works.
   No key, or `EVENTMATE_ENABLE_LLM=0`, means no model and the app behaves deterministically as before. Malformed keys are ignored, never sent.
2. Model ids live in `data/llm_models.json` (taken from the providers' public catalogs on 2026-10-30) with env overrides. Defaults: `nvidia/nemotron-3-ultra-550b-a55b` on NVIDIA,
   the same model's `:free` variant for OpenRouter heavy, `google/gemma-4-31b-it:free` for light. **Not yet run against a live key.**
3. Credit guard: every call is metered and failed calls count; a process budget (default 300) stops runaway loops; `max_tokens` is clamped (default 2000, cap 8000);
   temperature 0; reasoning blocks are stripped from replies. Errors name the provider and never contain a key.
4. `ANTHROPIC_API_KEY` and the `anthropic` import are gone from code, the env allow-list and the requirements. The assistant's large/small models now use the heavy/light tiers.
5. Tests make no real model call: the suite deletes every provider key before each test (conftest) and uses scripted clients. The only paid path is `python -m eventmate.smoke --llm`.
Found while doing this: the web app built its model client before `.env` was loaded, so keys in `.env` would not have been seen; `.env` is now loaded first.
Consequences: 26 tests in `tests/test_llm_providers.py`.

## ADR-033: Google Places for ratings, review counts and price band; DuckDuckGo stays the free search backup
Status: accepted (2026-10-30). Complements ADR-031.
Decision:
1. `places.py` (Places API (New) text search): rating, review count, price level 1-4 (a band, never a price), address, Google Maps link, and reviews only on request because they bill at a
   higher tier. Ranking uses a confidence-weighted rating (prior mean 4.0 worth 20 reviews) so a few perfect reviews do not beat hundreds of good ones. The key goes in a header; text is tag-stripped
   and capped; non-Google links are dropped; results cached 10 minutes; unconfigured means 503 with the variable name. Google attribution is carried in every result.
2. Web search: Brave when `BRAVE_SEARCH_API_KEY` exists, with DuckDuckGo automatically behind it (`FallbackSearch`); with no key DuckDuckGo alone runs, as the user's free setup needs.
Not verified live: no Places key yet (the key pasted earlier was truncated and was not stored). Field names follow Google's published reference.
Consequences: 26 tests in `tests/test_places.py`, 3 more in `tests/test_deals.py`.

## ADR-034: Free-text front door: one model call chooses one read-only search; code validates and runs it
Status: accepted (2026-10-30). Delivers the "AI-first" request without giving a model any facts or any power to change anything.
Decision: `explore.py` + `prompts/explore.route.v1.md`. The model sees today's date and the fenced request and returns one JSON object naming one of: deals, places, trip, flights, weather,
discover, or none. The reply is untrusted: parsed strictly, validated by the same models the forms use (extra fields refused, dates not in the past, city allow-list, airport codes), and
anything else is refused with a pointer to the forms. At most two providers are tried, 400 tokens each, light tier first. No searchable action writes, books, pays or messages anyone, so the
worst a hostile request can do is run a wrong read-only search. Prices, rankings and flags are never produced by the model. API `/api/explore`, `/api/explore/status`; an Explore view
(forms for deals, places, trips, weather, plus the free-text box) that works without a plan, and the forms work with no model key at all.
Consequences: 31 tests in `tests/test_explore.py`, a browser test that runs every tool against fakes. Not verified live: needs a model key.

## ADR-035: One smoke command exercises every integration live; the paid part is opt-in
Status: accepted (2026-10-30). `python -m eventmate.smoke` runs flights, trip search, deals, venue discovery, weather and places and reports PASS, WARN, SKIP or FAIL per integration with
timings, never printing a key. Unkeyed services SKIP; a busy public server WARNs; `--llm` additionally pings each model provider with 8 tokens and routes one sentence (at most about four calls,
counted and printed). The suite itself tests the smoke runner against fakes. First live run 2026-10-30: flights, trip, deals (DuckDuckGo) and weather passed; discover WARN (public Overpass server
unreachable); places and models SKIP (no keys yet).

## ADR-036: Hotels via the existing legitimate stack; the proposed scraper repos were vetted and not adopted
Status: accepted (2026-10-30). Answers the user's list of flight and hotel scraper repositories.
Evidence (each repo cloned to `_refs/` and read, nothing run): omkarcloud/google-flights-scraper (MIT) works by driving a patched Chrome (`patchright`) headed under a virtual display with sticky
proxies, built to pass Cloudflare's fingerprinting: that is bot-protection evasion, the same line ADR-028 declined to cross, and it duplicates fli, which already works. FlowExtractAPI/google-flights-scraper is
one README advertising a hosted Apify Actor: no code, not self-hosted, not free. factden/booking-com-scraper is a README, samples and a script that call a hosted Apify Actor (affiliate link in the README).
Anmoldureha/hotels-skill scrapes Booking.com with Playwright and a spoofed user agent and commits 4,159 `node_modules` files of unaudited code. restocktime/flight-deal-scraper (MIT) calls Kiwi Tequila; the portal
is reachable and the API returns 403 without a key, but current sign-up access and terms are unverified. None was adopted.
Decision: hotels use what already exists. `hotel` is a service for the deals agent (nightly prices extracted from public pages, `per_night` unit, bounds, optional `nights` turning a rate into a total) and for Google
Places (ratings, review counts, price band). Live run: hotel deals for Pune and Jaipur returned a few advertised rates, and MakeMyTrip and Goibibo were skipped because their robots.txt forbids it.
Real room rates and availability need an official API (for example Amadeus Self-Service Hotels, or a booking affiliate programme); that needs an account the user does not have yet and is the honest next step.
Consequences: 6 tests; explore prompt v2 knows hotels.

## ADR-037: Adversarial hardening pass: break each integration, fix what breaks, keep the attacks as tests
Status: accepted (2026-10-30). Requested by the user: "break each stuff, find all possible bugs, correct them, secure instinct".
Method: probes and fuzzers written to attack every integration (hostile servers, hostile pages, hostile model output, hostile requests), static analysis (pyflakes, bandit), a secret-leak scan of every log, plan, doc,
script output and the git history, and a live run of the real model against ordinary and hostile prompts. Every bug found is now a regression test (`tests/test_hardening.py`, `tests/test_fuzz_api.py`).
Found and fixed:
1. Deeply nested JSON from any server crashed five parsers with an unhandled RecursionError (models, weather, places, Overpass, Brave) and could crash the router and the request-body parser: `netutil.loads`.
2. `http.client`'s own errors (IncompleteRead, BadStatusLine...) are not OSErrors and escaped every client: `NETWORK_ERRORS`.
3. A server dribbling bytes could hold a worker open forever, since only per-read timeouts existed: every response read now has a total deadline (`read_capped`, `pinned_get(total_s)`).
4. One hostile search-result URL (malformed IPv6, a space, control or bidi characters, bad port) killed the whole deals run; one 140-character hostname failed report validation and discarded every offer. URLs are now screened,
   offers that cannot be built are dropped with a warning, and hostnames over 253 characters are refused.
5. A trip start of 9999-12-31 raised OverflowError; dates beyond 365 days now fail cleanly on every route, the CLI and the router.
6. A `.env` saved with a UTF-8 BOM (common from Windows editors) silently loaded nothing.
7. The assistant's policy-file validation used `assert`, which vanishes under `python -O` and would have loaded a malformed policy: replaced with explicit checks, and a test fails if any `assert` returns to the package.
8. Quota-spending GET routes (weather, places, discovery) had no CSRF protection, so a web page could drain the user's API quota with an image tag: they now require the CSRF header (the UI always sends it).
9. A pile-up of slow searches could occupy every worker: network-bound routes allow 4 at a time and answer 429 beyond that.
10. Real-model findings: the big reasoning model ran out of tokens thinking and returned no JSON for normal requests, and several models listed in NVIDIA's catalog answer 404 or time out. Routing now uses `gpt-oss-20b` with
    `reasoning_effort: low` and a 30 s timeout (3 to 10 s typical), a lone provider gets one retry, null means "not provided", and refusals say which field to fix.
11. The first fuzzer run was a false green (the rate limiter answered 429 before any handler ran); it now disables the limiter and a stats check confirmed the handlers are reached.
Held up under attack: no regex backtracking in any parser (200 KB hostile inputs in under 0.2 s), 16 numeric and DNS-alias SSRF tricks refused (`0x7f...`, `127.0.0.1.nip.io`, mapped IPv6, metadata hosts, credentials),
no secret in any log or in git history, hostile page and result text stays inert in the browser, web-facing modules provably import no model, shell or `eval` (AST test), and the real model refused or neutralised both prompt injections,
the key-extraction attempt, SQL-looking and smuggled-JSON requests.
Accepted risks: DuckDuckGo's HTML endpoint is unofficial; free-tier model endpoints have variable latency (seen 2 s to 46 s); the Overpass public server is still unreachable; a determined user can still ask the router to run
many legitimate read-only searches (bounded by the rate limit, the 4-at-a-time cap, the per-run model-call budget and the provider quotas). Bandit's remaining findings are `urlopen` on https-checked hard-coded or validated
hosts and a false positive on the bot name.
Consequences: 40 tests across `test_hardening.py` and `test_fuzz_api.py`; the full suite is 1,012 tests.

## ADR-038: Saved leads: a deal can be bookmarked against a plan, but never becomes a quote
Status: accepted (2026-10-30). Answers "turn a deals result into something trackable" without breaking ADR-025.
Context: Web offers are not tied to a vendor record in the database, and ADR-025 says a public listing never sets a price, so converting an offer into a quote observation would either need an invented
vendor id or let an unverified number price a plan.
Decision: `leads.py` stores `DealLead` bookmarks per plan in an append-only JSONL file (`<plan_dir>/leads/`, same guarantees as the ledger). Fields are strictly validated (http(s) URL without control
characters, text cleaned and bounded, low at most high, fixed unit and kind lists, source `user_input`). The same offer saved twice stays one lead. A lead is labelled "advertised price, not a quote", does not
enter the ledger, the Validator, replanning or any price path (tested: plan total, ledger and revalidation are unchanged). API `GET/POST /api/plans/<id>/leads` (POST needs CSRF); the Explore results show
"Save as lead" when a plan is open; the Budget tab lists the leads.
Not done: the server cannot verify that a saved offer really came from a search (the user supplies the fields), so a lead is the user's note, not evidence; no delete or edit (append-only, as everywhere);
turning a lead into a real quote still means the user asking the vendor and recording the written quote against a vendor in the database.
Consequences: 17 tests in `tests/test_leads.py`, one browser test.

## ADR-039: Weather advice is plain code over the forecast and the chosen venue's setting
Status: accepted (2026-10-30). Delivers the "weather drives a rain backup" item of F18 in advisory form.
Decision: `weather_advice.py` turns a forecast and the selected venue's setting (looked up in the vendor records, not guessed) into advice items: outdoor or unknown setting with rain at 30 percent or more
asks for a covered backup, tent price and decision cut-off; an indoor venue only gets an arrival note at 70 percent or more; a forecast high of 35 C or more outdoors suggests shade and water; otherwise
"nothing to prepare for"; with no forecast (the event is beyond the five-day horizon) it says so and claims nothing. Thresholds are constants, sentences are built from the forecast numbers, no model is used.
API `GET /api/plans/<id>/weather-advice` (CSRF-protected like every quota-spending GET); the Overview weather card shows the items. It changes no plan, version or price (tested).
Not done: re-timing the schedule around rain (needs F18's day-of copilot), cold-weather advice (no clear threshold for events), historical climate for dates past the horizon, per-hour advice.
Consequences: 8 tests in `tests/test_weather_advice.py`.

## ADR-040: Vendor score is a published formula over recorded quotes; missing data is left out, never guessed
Status: accepted (2026-10-30). Delivers F10 as far as the data allows.
Decision: `vendorscore.py` scores each vendor's latest unexpired quote in a category from three components, shown with their weights and a plain-language detail: price 50 percent (unit price against the median
of at least three current quotes with the same basis; per-plate quotes are compared per plate with known hidden costs spread over the guests; 80 percent of the median or less scores 100, 150 percent or more scores 0),
completeness 30 percent (checklist items covered, each unquantified hidden cost counts as a gap), evidence 20 percent (source type base 100/80/50/20 times the recorded confidence). A component without enough data
is left out and the overall is re-weighted over the rest; `coverage` states how much of the formula that was. A public listing is never priced or used for the median (ADR-025). A price under half the median is
flagged as "unusually low", not rewarded beyond 100. Expired quotes are unscored with a re-quote repair. `not_scored` always names response time, terms and cancellation, and ratings, because the data model holds none.
API `GET /api/observations/score/<category>`, CLI `quotes score CATEGORY`. Read-only: no plan, price, log or file changes (tested).
Not done: (UI card added the same day, see the handoff); the thresholds (3 quotes, 0.8/1.5 ratios, evidence bases) are my choices, unvalidated against real procurement outcomes; response time and terms need new recorded fields; ratings
(Places) are not tied to vendor records, so they stay out of the score.
Consequences: 20 tests in `tests/test_vendorscore.py`.

## ADR-041: Quote requests by email and WhatsApp are drafted by EventMate and sent by the user; nothing is sent from here
Status: accepted (2026-10-30). Delivers the safe part of F11 at the user's request ("email and WhatsApp, no WhatsApp API, free"). Refines ADR-018; the sending part of F11 stays blocked.
Context: The user wants quote requests to vendors over email and WhatsApp without the paid WhatsApp Business API. The free routes are click-to-chat (`https://wa.me/<number>?text=...`) and `mailto:` links, both opened in
the user's own apps. The other free option, driving a personal WhatsApp account with an unofficial library (whatsapp-web.js, Baileys and similar), breaks WhatsApp's terms and gets numbers banned, so it is rejected.
Decision: `outreach.py` writes a fixed-template message from the plan's validated facts (vendor name from the selection, event type, city, date, guest count, dietary needs for caterers, the quote checklist, a request for validity and
availability) and a link. No model writes it; the budget, plan cost and allocation are never in it (tested). The contact is typed by the user per draft: emails must be one plain address (no names, lists, `?` or `&`, so no
header or parameter injection), phone numbers are normalised to digits with a country code (a bare 10-digit number starting 6-9 is treated as Indian and gets 91; anything else needs + or 00, otherwise it is refused).
Message and subject are percent-encoded, so hostile sender text cannot add parameters (tested). The link is restricted by the schema to `mailto:` or `https://wa.me/<same digits>?text=`. Drafts are append-only JSONL per plan
(`<plan_dir>/outreach/`, 0600); "I sent it" appends a `marked_sent` record and is the user's word, labelled so. A draft changes no plan, version, price or ledger entry. API `GET/POST /api/plans/<id>/outreach`,
`POST .../outreach/<draft_id>/sent` (CSRF), CLI `outreach`, a Budget-tab card ("Ask vendors for a quote"). A reply still has to be recorded by the user as a quote with evidence (ADR-025).
Not done: SMTP or any automatic sending, reading replies, follow-up reminders, Hinglish or other languages (English only; no vendor language was specified, ADR-018), a contact book (vendor records hold no phone or email),
a check that the number is on WhatsApp. The vendor's phone number and email are personal data stored in the outreach file; DPDP retention and deletion are not handled (pre-launch blocker 3). The user remains responsible
for only contacting vendors they may contact (DND/TRAI questions in the roadmap's blockers apply to automation, not to a person tapping send, but that is the user's check, not mine).
Consequences: 37 tests in `tests/test_outreach.py`, one browser test.

## ADR-042: The agent talks to vendors itself (email and WhatsApp), behind an approval gate, and reports back in an inbox
Status: accepted (2026-10-30). Supersedes the "you send it" part of ADR-041 at the user's direction ("the agent should communicate and respond back, like OpenMuse"). ADR-018's rule that nothing is sent without the user's say-so is kept, as an approval step instead of a copy-paste step.
Study of OpenMuse (MIT, commit a4944f4, read 2026-10-30): it does not message people on its own. Its pattern is a durable task engine (leases, heartbeats, compare-and-swap status changes, run events), an action proposal for every outbound step
(content hash, 30-minute expiry, idempotency key, approve or deny once, execution claimed before it runs, outcome_unknown when the result cannot be known), a worker loop that does the slow part, and an in-app notification inbox.
Gmail send and thread reading are adapters behind that gate; WhatsApp is listed there as future work. No code was copied; the patterns were re-implemented in Python.
Decision:
1. Channels (`channels.py`): email over SMTP (TLS or STARTTLS, never plain) and IMAP (read-only select, `BODY.PEEK`, so mail is not marked seen); WhatsApp through WAHA (Apache-2.0, commit c42f68a, read 2026-10-30; self-hosted REST gateway over a WhatsApp Web session):
   `GET /api/checkNumberStatus`, `POST /api/sendText`, `GET /api/{session}/chats/{chatId}/messages`, `X-Api-Key` header. This is not the official Business API and WhatsApp's terms do not allow unofficial clients, so the number can be restricted; the UI says so and
   the code sends slowly, only after approval and only to numbers WhatsApp reports as registered. The user chose this route knowing that.
2. Approval gate (`outreach.py`, `conversations.py`): every message is a proposal bound to a SHA-256 of plan, vendor, channel, recipient, subject, body, kind and parent; approval must present that hash; proposals expire after 24 hours; approve and deny act once; a record whose
   content no longer matches its hash fails to load (fail closed, tested). Statuses: prepared, approved, sending, sent, failed, outcome_unknown, denied, expired, marked_sent. The claim `approved -> sending` is a compare-and-set, so two dispatchers cannot both send.
   An unknown outcome (a connection lost mid-send, a gateway answer without an id) and a send left in `sending` after a crash are never retried; the UI says to check the chat.
3. The agent writes only fixed templates or the user's own words (follow-ups, 1000 characters, cleaned); it never offers a price, accepts one or mentions the budget. Suggested follow-ups are fixed questions chosen from what the reply left unanswered.
4. Limits are code: 3 messages per vendor and 30 per day (24-hour window), WhatsApp messages at least 20 seconds apart (a queued draft waits for the next round), 5 follow-ups per thread, and a 5-second floor on the polling interval.
5. Replies are untrusted data. Poll matches an email by its In-Reply-To/References against the sent Message-ID, then by sender address; WhatsApp by number; strangers, robots (Auto-Submitted, Precedence, List-Id), mailing lists, our own mail and replies older than the request match nothing. Text is cleaned (quoted history cut, control characters
   removed, 4000 characters), attachments are counted and never opened, and a rule-based analysis quotes the vendor's own words: INR amounts (the deals regex), "not available" or "available" hints, questions asked, checklist items mentioned. Nothing in a reply records a quote, changes a plan, approves or sends anything (tested with hostile text);
   "Record as quote" only pre-fills the existing form with the amount and an evidence reference, and the user still records it (ADR-025).
6. The worker (`create_app`): a daemon loop sends approved drafts and polls every channel, default every 60 seconds once any channel is configured (`EVENTMATE_POLL_SECONDS`, 0 = off); a failing round is logged by type and the next one retries; a failing channel is reported without stopping the other. CLI: `inbox show|poll|approve|deny`, `inbox poll --watch SECONDS`.
7. The user's view: a Vendor chats tab with a badge (unread replies plus drafts waiting for approval), channel readiness, saved vendor contacts (typed by the user; vendor records hold none), "draft requests for all vendors with a contact", per-thread timeline, approve or decline, suggested or custom follow-ups, and the manual fallback (link plus "I sent it") when a channel is not set up.
Verified: unit tests with fake SMTP, IMAP and WAHA transports; a real SMTP exchange against a local server (envelope, Message-ID, In-Reply-To/References, UTF-8 body; reply parsed back and matched); Chromium tests of the manual path and of approve, send, reply, analysis and pre-filled quote.
Not verified: login and TLS against a real mail provider, a real IMAP server, any real WAHA session (needs a phone to scan the QR), WhatsApp's behaviour under real use (restrictions, LID chat ids), Gmail's handling of repeated app-password logins. Run `python -m eventmate inbox poll` once with real settings before relying on it.
Not done: a standing mandate for auto-replies without approval (kept out on purpose: nothing leaves without an approval; a mandate would need its own limits and a kill switch), attachments, voice, languages other than English, delivery or read receipts, scheduled reminders to vendors who do not answer, retention and deletion of vendor contact data and chat text (DPDP, pre-launch blocker 3), and the legal checks of ADR-018 for contacting vendors this way (the user owns them).
Consequences: 77 new tests (`test_channels`, `test_conversations`, `test_conversations_api`) plus two browser tests; 1168 pass. `eventmate/env.py` reads the new names (secrets only from the environment; reprs and statuses never contain them).

## ADR-043: Standing permission for automatic follow-up questions, narrow, bounded and revocable
Status: accepted (2026-10-30), at the user's request ("yes I need it") after ADR-042 left it out on purpose. Refines ADR-042's rule that every message needs a per-message approval: this is the one exception, and the user grants it.
Decision: a per-plan `Mandate` (`schemas.py`, `outreach.py`, `conversations.py`) lets the agent answer a vendor's reply with ONE fixed follow-up question about something still missing, without asking each time.
1. Scope: only the fixed questions of `suggest_followups` (price and what it covers, checklist items not mentioned, availability on the date, validity of the quote). Never a price, an acceptance, a commitment, the budget, or an answer to something the vendor asked.
2. Hands over to the user, and marks the reply handled, when the reply contains a question for the user, says they are unavailable, includes an attachment, or asks to stop ("stop", "do not contact me", "wrong number", "not interested", "remove my number"; any earlier stop request blocks all later automatic messages in that thread),
   or when nothing is left to ask. A reply is looked at once; a reply that arrives while no permission is active can never be answered automatically later.
3. Bounds (checked again when the message is sent, not only when it is queued): active for 1-168 hours (default 72), at most 3 per vendor (1-5) and 10 per plan (1-30), only between 09:00 and 20:00 IST (a queued message waits), plus the ordinary limits of ADR-042 (per vendor, per day, WhatsApp spacing).
4. Brakes: revoke for one plan, or the global kill switch (a `KILL` file in the plan directory, so it survives restarts). Either one denies messages already queued under the permission, and the kill switch also refuses new grants. Expiry does the same.
5. Honesty to the vendor: an automatic message ends with "(Sent by an automated assistant on behalf of <name>.)". The draft records `approved_by: mandate` and the UI shows "sent automatically".
6. Interfaces: API `GET/POST /api/plans/<id>/autoreply`, `POST .../autoreply/revoke`, `POST /api/autoreply/kill|resume`; CLI `inbox autoreply PLAN [--name N --hours H --max-per-thread N --max-total N | --off]`, `inbox kill`, `inbox resume`;
   a panel at the top of the Vendor chats tab with a permanent "STOP all automatic messages now" button.
Not done: per-vendor opt-in (it is per plan), an operator allow-list of vendors, a daily digest of what was sent automatically (the inbox shows it), replies that need judgment such as negotiating, a time zone other than IST (a fixed UTC+5:30 offset), vendor-side detection of other automatic replies on WhatsApp (an auto-responder bot could answer our questions; the caps and the one-answer-per-reply rule bound that loop but do not detect it).
Risks accepted by the user: a mistaken classification can send a question the user would not have; the vendor may dislike automated messages; on WhatsApp through WAHA the account can be restricted (ADR-042).
Verified: 27 tests in `tests/test_autoreply.py` (scope, hand-over cases, caps, window, expiry, revoke of queued messages, kill switch, failed sends not retried, hostile reply text cannot widen the permission, API, CLI) and a Chromium test that was checked by mutation (removing the kill-switch click makes it fail).

## ADR-044: Follow-through jobs: a durable goal over many vendor threads, price claims the user confirms, one permission
Status: accepted (2026-10-30). An outside design and code slice (`EventMate-followthrough`: Job, QuoteClaim, worker) was reviewed and adopted with changes; the design lives on in `docs/JOBS_DESIGN.md`.
Decision: add `jobs.py` (SQLite, compare-and-swap on a version, legal transitions, history), `claims.py` and `worker.py`. A Job is "N comparable quotes from these vendors by a deadline": it drafts one request per vendor (the user approves the exact texts), watches replies, proposes prices as claims, drafts or, under the standing permission, sends a fixed nudge when a vendor is silent, marks vendors unresponsive, asks the user for everything else, and finishes as done, expired or cancelled.
A claim becomes a quote only when the user confirms it (ADR-025 holds); the user states how long it counts as valid.
Changes from the delivered code, each found by review or by a failing test: (1) its separate `Grant` was replaced by the standing permission of ADR-043 (it would have bypassed the kill switch, the window and the disclosure); (2) confirming a claim silently assumed 14 days of validity, now the user chooses and the vendor's stated validity is only suggested;
(3) its amount check let a bare "66" vouch for 66,000, now a multiplier counts only when written; (4) `OutreachStore` read-check-write steps were not safe across processes, so a separate worker process plus the web server could double-send (12 of 12 processes claimed one send in a test), now an exclusive file lock, tested with real processes and checked by removing the lock;
(5) a queued nudge went out a tick late, so the worker dispatches again after its jobs; (6) a naive deadline crashed with a TypeError, now every timestamp must carry a time zone; (7) a cancelled job left automatic messages queued, now they are denied and dispatch vetoes them independently; (8) both a job and the permission could answer one reply, now one claimant per `action_key`; (9) the recipient is snapshotted per job.
Interfaces: API `GET/POST /api/plans/<id>/jobs`, `POST /api/jobs/<id>/approve-requests|cancel`, `.../claims/<c>/confirm|reject`, `.../items/<i>/resolve|approve-nudge`, `POST /api/jobs/tick`; CLI `jobs show|start|approve|confirm|reject|resolve|nudge|cancel|tick` and `python -m eventmate.followthrough.worker --once|--every N` (stop file in `EVENTMATE_WORKER_STOP`);
the web process runs the same round in its background loop when any job exists; a Quote jobs panel in the Vendor chats tab.
Measured: a 32-reply English and Hinglish labelled set written for this project (not real vendor data): 19 replies hold a price worth recording, 17 were found, 2 missed (both went to the user), 0 wrong prices proposed. The set shows the guard works; it says nothing about real replies.
Not done: notification of the user outside the UI (a digest text exists, sending it is a separate step), PDF quotes, LLM extraction, automatic next candidate, automatic replan after a confirmation (re-pricing stays a manual click), webhooks, per-job token budgets (no model is used), stuck-job escalation after 72 hours.
Consequences: about 100 new tests (`test_jobs_worker`, `test_jobs_api`, `test_claims`, `test_multiprocess`) and a Chromium test.

## ADR-045: Personalized ideas are guarded suggestions, never facts
Status: accepted (2026-10-31). Fits the brief item "personalized ideas".
Decision: `ideas.py` makes `Idea`/`IdeaSet` (frozen, `extra="forbid"`, source `llm:<model>` or `rule:idea_templates`, status always "suggestion"). Inputs are verified plan facts only (event type, guest count, a budget band, diets, city, venue setting, up to 5 user preferences); the raw brief and the budget never reach a model. Templates (`data/idea_templates.json`, versioned, `reviewed: false`) work with the model off and are deterministic. A model may add ideas; each passes `guard_idea`: no price or currency (cost notes are words with no digits), no business name not in the database, no availability or capacity claim, no tradition/ritual/religion claim, no markup or link, only known fact names. Failed ideas are dropped and the reason recorded. Prompt `prompts/ideas.generate.v1.md` (wording and JSON shape only) is hash-registered under `other_prompts` in `assistant.lock.json`. API `GET/POST /api/plans/<id>/ideas`, pin, dismiss, restore, note done; an Ideas tab. Pinning writes a note or checklist item beside the plan (`<plan_dir>/ideas/`); plan versions, the validator and the ledger are untouched (tested).
Not done: behavioural evals of the idea prompt on a real model, ranking by taste, images. The budget-band thresholds (under 5%, under 20% room) are my choice.
Consequences: 44 tests in `test_ideas.py` plus a browser test.

## ADR-046: Community events as a profile-driven type with three separate requirement classes
Status: accepted (2026-10-31).
Decision: `EventType.COMMUNITY` (OTHER kept) with optional `CommunityDetails`. Everything that varies by kind of community event is data in `data/community_profiles.json` (keywords, budget shares, event window, suggested roles, always-shown items); the demo subtype is a configuration choice and the default is `other`. Editing the file (or `EVENTMATE_COMMUNITY_PROFILES`) changes behaviour with no code edit (tested).
A. Hard checks computed by the Validator from user input and the database, fail-closed with repairs: `attendance_vs_capacity` (top of the user's range, headroom shown), `funding_vs_budget` (own funds + received; committed counted only if the user opts in and shown as such; pledged never counted), `volunteer_coverage` (only roles the user confirmed; no double booking; named setup and cleanup owners), `timeline_dependencies` (programme activities are tasks in the existing CP-SAT model with shared resources and "after" order), `accessibility_declared` (a venue with no data is `unverified`, neither pass nor fail; the user records the venue's answer with evidence). Allergens are not checked: at most a class B item.
B. Conditional requirements: shown from the user's answers (only "no" hides one), each saying "Check with <who>", ending `confirmed_by_user`, `not_applicable_by_user` or `not_verified`; only a user action changes one; the profile loader refuses data that says safe, compliant, legal or mandatory; the summary reads "N conditional items not verified".
C. Suggestions: ideas (ADR-045).
`ValidationReport.unverified` is new (not a pass, not a failure). Intake maps keywords to COMMUNITY and flags the type for the user to check. Budget shares come from the profile; `cap_weights` still win. New disruption `community_update` makes any change a validated new version, or a refusal with repairs.
Found on the way, not changed: a 09:00 conference start is unschedulable under the default timing policy (setups chain from 08:00). The scheduler now explains it (`event_start_too_early`, earliest start 10:30 or 12:00) instead of a misleading message.
Not done: reviewed rule packs (ROADMAP F7), IANA time zones, multi-day, real permit or licence rules (deliberately none).
Consequences: about 130 tests across `test_community*.py`, `test_intake_community.py`, plus a browser test.

## ADR-047: Email flow end to end with fakes, owner digest, and a mailbox smoke check
Status: accepted (2026-10-31).
Decision: (1) `tests/test_email_flow_e2e.py` runs the real `EmailChannel` against a fake SMTP server and fake IMAP mailbox (`tests/mailfake.py`) through brief, plan, job, approval, plain/Hinglish/ambiguous replies, claim, confirmation and digest, and shows the plan unchanged until the user presses the existing re-price. Replies that repeat, come from another sender (flagged, no claim, never answered automatically), quote history, robots, unknown send outcome, kill switch, window and caps are covered. (2) Bug fixed: the worker raised an item for a reply the standing permission had already answered; it now watches the permission's answer and returns the reply to the user if that answer is denied or fails. A question in an earlier reply of the same round also blocks an automatic answer. (3) Digests are always written to the in-app inbox when content changes. An owner email copy is off by default; enabling is two steps (propose the exact address and scope, then confirm with a code bound to them); any change returns it to pending; vendor addresses and the sending mailbox are refused; the email passes the kill switch, stop file, 09:00-20:00 IST window, 4 per day and 30 minute gap, is logged without content, an unknown outcome is never resent, and it never contains the budget or a money-bearing goal. (4) `python -m eventmate.messaging.smoke_email` checks logins read-only and prints only PASS/FAIL and host. (5) `EMAIL_SETUP.md`, `EMAIL_VALIDATION.md` (not yet run).
Not done: any real-mailbox run (Task 4 is the user's), WhatsApp changes, attachments.
Consequences: tests `test_email_flow_e2e`, `test_digest_notify`, `test_smoke_email`.

