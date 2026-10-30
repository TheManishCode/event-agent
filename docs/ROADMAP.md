# EventMate roadmap and feature triage

Status values: **Accept** (scheduled), **Defer** (good, not yet, reason given), **Reject** (reason given).
Rejections are binding until a new ADR supersedes them. Decisions: `DECISIONS.md`. State: `HANDOFF.md`.

## Positioning

EventMate is a constraint-verified, explainable event copilot for Indian weddings, birthdays and
community events. It turns a brief into a plan that is checked against budget, capacity, date, dietary and
vendor constraints, offers ranked trade-offs when they conflict, and replans without breaking confirmed bookings.
Others generate suggestions; EventMate proves feasibility and shows its work. Novelty is the combination:
solver-verified plans, provenance-tracked vendor quotes, bounded negotiation, and a secrets-free agent wallet.

## Resolving the five known limitations

| Limitation | Production solution | ADR | Phase |
|---|---|---|---|
| Budget rebalancing manual | Trade-off Negotiator: ranked, priced, verified options; user applies via Replanner | 003 | 1 |
| Single organizer, local time | IANA zones, availability windows, organizers as a Cumulative resource, multi-day | 004 | 2 |
| English + INR only | Locale packs (lakh/crore, ₹, Devanagari digits, Hinglish) + confirm-the-parse card for LLM-proposed parses | 005 | 2 |
| No real vendor data | Observation ledger with expiry/confidence; written vendor confirmation | 006 | 3 |
| Local single-user server | Gunicorn + TLS proxy, OIDC users, tenant-scoped SQL store, CSRF, rate limits | 014 | 3 |

## Feature list

| # | Feature | Decision | Phase | Notes / reason |
|---|---|---|---|---|
| F1 | Trade-off Negotiator (ranked options: harm, extra spend, verified) | Accept (done 2026-10-30) | 1 | ADR-015; CLI `tradeoffs`, API `/api/tradeoffs`, UI card |
| F2 | Budget Reality Check up front | Accept (done 2026-10-30) | 1 | ADR-016; API `reality`, UI card, CLI `plan` (stderr); `eventmate/pricing/reality.py` |
| F3 | Live budget ledger (planned / quoted / paid / pending / variance) | Accept (done 2026-10-30) | 1 | ADR-017; CLI `ledger`, API `/api/plans/<id>/ledger`, Budget-tab card; payments are user-entered, never processed |
| F4 | "Why?" panel in UI for every recommendation | Accept (done 2026-10-30) | 1 | Vendors tab button; shows hard constraints checked, confidence, source from the decision log |
| F5 | Timezone/calendar-aware, multi-organizer scheduler | Accept (multi-organizer done 2026-10-30; zones, working hours, multi-day open) | 2 | ADR-004, ADR-020 |
| F6 | Locale layer + parse confirmation | Accept (locale pack v1 done 2026-10-30; confirmation card open) | 2 | ADR-005, ADR-019 |
| F7 | Cultural/dietary rule packs (rituals, cuisines, Jain/vegan/accessibility) | Accept (dietary checklists done 2026-10-30; traditions and accessibility open) | 2 | ADR-021; versioned data, not LLM text; tradition packs need named reviewers |
| F8 | Constraint compiler (OR-Tools CP-SAT + PDDL) | Already built | - | Keep; do not add another planner |
| F9 | Observation ledger + quote intelligence (inclusions, hidden costs, missing items, validity) | Accept (done 2026-10-30 for local use: store, quote pricing, UI card, vendor-confirmation label) | 3 | ADR-006, ADR-025 |
| F10 | Vendor score (transparent formula: price vs observed median, response time, quote completeness, terms) | Accept (partial 2026-10-30: price, completeness, evidence; response time and terms need data; Budget-tab card done; ADR-040) | 3 | Formula shown to user; ratings only if a sourced observation exists |
| F11 | Email + WhatsApp quote requests, written confirmation | Accept (built 2026-10-30, ADR-042: agent sends approved messages through the user's own mailbox or a WAHA gateway and reads replies; not run against real accounts; automatic follow-up questions under a standing permission, ADR-043) | 3 | Legal check of ADR-018 is the user's; WhatsApp via an unofficial gateway can get the number restricted |
| F12 | Credential Wallet (grants, approvals, audit, revoke-all) | Accept | 3 | ADR-009; prerequisite for F11 sending |
| F13 | Human approval queue for outbound messages, data sharing, new accounts | Accept | 3 | Part of F12 |
| F14 | Bounded text negotiation | Accept | 4 | ADR-008 |
| F15 | Adaptive replanning on price/expiry refresh | Accept (jobs ADR-044 produce confirmed quotes; replanning on them is still a manual re-price) | 4 | Expiring observations trigger replan proposals |
| F16 | Guest intelligence: headcount estimate (confirmed + probable), food quantity, reminders | Accept | 4 | Estimates labelled; rates are configurable data, not claimed accuracy |
| F17 | Seating optimiser | Defer | 5 | Fits CP-SAT but needs relationship data input; low value before guests flow exists |
| F18 | Day-of copilot: live run sheet, delay re-timing, team updates | Accept | 5 | Weather only from a real weather tool, never LLM (OpenWeather forecast card and API done 2026-10-30, ADR-029; not yet feeding day-of re-timing) |
| F19 | Read-only browser discovery | Defer | 5 | ADR-010; hostile-page tests first |
| F20 | Google Sheets/Calendar export | Accept | 4 | ADR-011 |
| F21 | Voice negotiation pilot (10-20 opted-in vendors, 1 city, 1 language) | Defer | 6 | ADR-007; legal and speech-quality risk; revisit with real vendor audio |
| F22 | Local RAG over contracts/past events (Ollama + vector store) | Defer | 6 | Useful only after real contracts exist; also a prompt-injection surface |
| F23 | Production serving and auth | Accept | 3 | ADR-014 |
| F24 | Assistant: RAG, permissioned tools, proposals, cache and router, traces, change-controlled prompt | Accept (v1 done 2026-10-30; behavioral evals not yet run on a real model) | 3 | ADR-023; `eventmate/assistant/`, Ask tab |

| F25 | Personalized ideas (guarded suggestions) | Accept (done 2026-10-31, ADR-045) | 3 | Templates plus optional model; never prices or claims |
| F26 | Community events with profiles, hard checks and user-marked conditional items | Accept (done 2026-10-31, ADR-046) | 3 | No rule packs: conditional items stay not_verified until the user marks them |
| F27 | Owner digest in-app plus optional email copy; mailbox smoke check | Accept (done 2026-10-31, ADR-047) | 3 | Real-mailbox validation still to run (docs/EMAIL_VALIDATION.md) |

## Rejected

| Proposal | Reason |
|---|---|
| Rename to EventMind | ADR-001: churn, no capability |
| LangGraph rewrite / 8-LLM-agent swarm | ADR-002: breaks deterministic guarantees; existing agents already cover planner, budget, vendor, guest, scheduler, validator |
| LLM "Guardrail Agent" | Guardrails must be code. Replaced by the deterministic policy engine in the wallet and negotiation mandate |
| Automatic budget rebalancing | ADR-003: would silently override hard caps |
| Storing vendor-portal passwords | ADR-009: prefer magic links/OAuth; large liability for little value |
| Storing card/payment data; auto-pay; auto-signing contracts | Never. Payments and contracts are always human actions |
| Persisting browser cookies/sessions | High risk, no current need |
| Gemini Auto Browse / Workspace Gemini as execution layer | ADR-010/011: not under our validator or guardrails |
| Cold calling or messaging non-opted-in vendors | ADR-007: legal and number-flagging risk |
| Fabricated competing quotes or deadlines in negotiation | ADR-008: dishonest |
| "Zero-exposure" marketing claim | Overclaim; the backend handles secrets. Say "the model never sees them" |
| Playlists and trip-style travel features (from `hitched`/trip-planner) | Out of scope for the product. Flights were rejected here but the user asked for them (ADR-028): fares and schedules only, no booking |
| Predicting attendance with unvalidated ML | No data to train or validate; use configurable-rate estimates, labelled |

## Pre-launch blockers (human to verify; not checked in this session)

1. WhatsApp Business API policy, template approval and pricing.
2. TRAI/DND/DLT applicability to automated messages and calls; call-recording consent.
3. DPDP Act: vendor-staff and guest personal data, retention, deletion.
4. Google OAuth app verification for Gmail scopes.
5. Licences of every reference repo before borrowing code (ADR-013).

## Phase exit criteria

Each phase ships only with: tests written first (including adversarial), all earlier tests green, one
end-to-end run, and updated `HANDOFF.md` + `DECISIONS.md`.
