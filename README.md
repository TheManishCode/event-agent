# EventMate

**A constraint-verified planning copilot for weddings, birthdays and community events.**

EventMate turns a plain-language brief into a plan with a budget, a timeline and a vendor checklist, and it only shows you a plan after an independent validator has re-checked every hard constraint. When constraints conflict it offers priced trade-offs instead of silently bending them. It can also write to vendors for you, read their replies, and keep a job moving over days, while every price stays a proposal until you confirm it.

Language models are used where wording helps (reading your brief, suggesting ideas, answering questions, rewording explanations) and deliberately **not** where facts matter: plan validity, prices, availability, capacity and legal or cultural claims come from code, data, or you.

> **Status: early.** Vendor data is synthetic seed data. Email is tested with fake SMTP/IMAP only and has not been validated against a real mailbox. Treat this as a working prototype, not a production service.

## What it does

| Area | Capability |
|---|---|
| **Plan** | Brief → parsed request → vendor shortlist → CP-SAT schedule → PDDL cross-check → independent validation. Immutable plan versions with diffs |
| **Budget** | Allocation with a contingency reserve, a reality check up front, a live ledger, and ranked, verified trade-off options |
| **Replan** | Vendor cancellations, budget, guest-count and date changes produce a new version; confirmed bookings are preserved unless you release them |
| **Ideas** | Personalized suggestions from verified facts; templates work with no model; model ideas are guarded (no prices, no invented vendors, no availability or cultural claims) |
| **Community events** | Profile-driven (college fest, neighbourhood, fundraiser, local cultural). Hard checks for attendance, funding, volunteers, timeline and accessibility; conditional items (permits, noise, food licence…) that only you can mark |
| **Quotes** | Ledger with provenance and expiry, hidden-cost flags, and a transparent vendor score |
| **Find vendors** | Finds businesses and public contact details from their own pages; you review and confirm each address; then EventMate sends and tracks replies. Discovered businesses are kept apart from the vendor database, and a do-not-contact list covers every plan and job |
| **Vendors** | Hash-approved messages over email (SMTP/IMAP) or WhatsApp (self-hosted gateway, experimental); replies read as untrusted text; optional narrow standing permission with caps, window and kill switch |
| **Jobs** | "Get N comparable quotes by a deadline": claims you confirm, nudges, expiry, an in-app digest and an optional email copy to you |
| **Assistant** | Grounded answers with sources, permissioned tools, proposals that need your click |
| **Interfaces** | Local web UI, REST API, and a CLI |

## Quick start

Requires Python 3.12+ (developed on 3.14).

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # or: pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
python -m eventmate.web            # UI at http://127.0.0.1:8765
```

```sh
python -m eventmate plan "Plan a 120-person engagement party in Pune on December 12 under INR 200,000. Vegetarian catering, indoor venue, photographer."
python -m eventmate show <plan_id>
python -m eventmate replan <plan_id> --cancel-vendor cat-pune-rasoi
```

**Solvers.** Planning runs a PDDL cross-check with Fast Downward and VAL. Point at them with `EVENTMATE_FD` and `EVENTMATE_VAL` (see [docs/CONFIGURATION.md](docs/CONFIGURATION.md)). Without them planning stops with an explicit "symbolic check unavailable" failure; `--allow-no-symbolic` skips only that check and says so.

## Architecture

```
brief ─ Manager ─ parse ─▶ EventRequest ─ decompose ─▶ task graph
                              │
   Guest ── RSVPs, diets ─────┤
   Budget ── allocation ──────┤──▶ Vendor ── shortlist (every hard constraint, every reason)
                              │
   Planner ── vendor set ── Scheduler (CP-SAT) ── PDDL check
                              │
   Validator ── re-derives every fact from the database; no valid plan, no PlanVersion
                              │
   Plan store (immutable, hash-checked)     Explainer (text with reasons and alternatives)
                              │
   Replanner ── disruption ─▶ new version + diff (added / removed / changed / preserved)
```

```
eventmate/
  schemas.py  service.py          typed contracts and the facade everything goes through
  agents/                         manager, budget, guest, vendor, planner, scheduler, validator, replanner, tradeoff, explainer
  planning/                       community events, ideas, rule packs, conversational intake
  pricing/                        quote ledger, vendor score, budget ledger, reality check
  messaging/                      email/WhatsApp channels, approvals, conversations, digests, owner notifications
  sourcing/                       discovered businesses, public contact extraction, shortlists, the do-not-contact list
  followthrough/                  jobs, price claims, the background worker
  integrations/                   flights, weather, places, venue discovery, deals search, explore (read-only)
  assistant/                      grounded Q&A, tools, approvals, evals
  web/                            Flask API and single-page UI
  data/                           seed vendors, profiles, idea templates, knowledge base
prompts/                          versioned prompt files (wording and output format only)
tests/                            1696 tests
docs/                             decisions, roadmap, setup guides, design notes
```

## Guarantees (each is enforced and tested)

- **No invented facts.** Availability, prices, capacities, diets and booking status come from the database, you, or a tool. A model may phrase or propose; it never sources a fact.
- **Validated or not shown.** A `PlanVersion` cannot be constructed unless every hard constraint passed an independent re-check.
- **Failures explain themselves.** Every failure names the violated constraint and a concrete repair.
- **Nothing is overwritten.** Every replan writes a new immutable version and a diff.
- **Nothing leaves without approval.** Vendor messages are bound to a hash of their exact text. The only exception is an optional, capped, time-boxed standing permission for fixed follow-up questions, with a kill switch. Request templates and automatic follow-ups never contain your budget or a price; text you type yourself is sent as written.
- **Nothing found is used until you confirm it.** An address read from a business's own page or listing is a candidate with its source. Only an address you confirm can be written to, and a stop request or a permanent delivery failure for a message we sent puts it on a do-not-contact list that only you can lift. Discovery depends on what a source publishes and coverage varies by city and category. EventMate does not check anti-spam or privacy law; check the rules that apply to you before contacting businesses.
- **Replies are untrusted data.** A vendor reply never records a quote, changes a plan or approves anything.
- **Conservative sending.** A send is claimed before it happens (a file lock across processes), and a send whose outcome is unknown is never retried automatically. This reduces the chance of duplicates; it cannot make one impossible.
- **Provisional until verified.** If a need you stated cannot be checked from the data (for example a required access need the venue has not declared), the plan is built but marked **Needs verification** everywhere; it is not called verified until you record the venue's answer. Conditional requirements (permits, noise rules, licences…) stay "not verified" until you mark them; EventMate never says a plan is safe or compliant.

## Testing

```sh
python -m pytest                  # whole suite
```

1696 tests passed on Python 3.14.8 with Fast Downward, VAL and Chromium available. Without the solvers most planning tests fail by design; without Chromium the browser tests skip. Tests use fake SMTP/IMAP and fake channels and never call a keyed or external service.

## Documentation

| | |
|---|---|
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | environment variables, API keys, optional integrations |
| [docs/EMAIL_SETUP.md](docs/EMAIL_SETUP.md) | mailbox setup, limits, how to stop everything |
| [docs/EMAIL_VALIDATION.md](docs/EMAIL_VALIDATION.md) | the plan and record for a real-mailbox test (not yet run) |
| [docs/DEMO.md](docs/DEMO.md) | a five-minute walkthrough |
| [docs/DECISIONS.md](docs/DECISIONS.md) | architecture decision records |
| [docs/ROADMAP.md](docs/ROADMAP.md) | what is built, deferred and rejected |
| [docs/JOBS_DESIGN.md](docs/JOBS_DESIGN.md) | follow-through jobs design |
| [docs/HANDOFF.md](docs/HANDOFF.md) | current state for the next contributor |
| [CONTRIBUTING.md](CONTRIBUTING.md) | how to contribute |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [AI_CODING_RULES.md](AI_CODING_RULES.md). Every change ships with tests, including at least one adversarial test for each new input surface.
