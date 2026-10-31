# EventMate handoff

Read this first, then `AI_CODING_RULES.md`, `DECISIONS.md`, `ROADMAP.md`. Update this file in the same
change as any code or decision change, and at the end of every session. Stale handoff = unfinished work.

Last updated: 2026-10-31
Updated by: Claude (ideas ADR-045, community ADR-046, email flow ADR-047)

## Current state

- Package: `eventmate/` (sibling of `src/llm_plan`, which is untouched). Python venv at `.venv`.
- Tests: `python -m pytest tests` → **1512 passed** (Python 3.14.8, with Fast Downward, VAL and Chromium) (2026-10-31; includes the Chromium end-to-end tests). pyflakes clean.
- Git: this repository (`event-agent`) is the home of the code. Tests live in `tests/`; the package is grouped into `planning`, `pricing`, `messaging`, `followthrough`, `integrations`, `agents`, `assistant`, `web`.

### Roadmap progress (rows count only with passing tests)

| Item | State | Where |
|---|---|---|
| F1 Trade-off Negotiator | done | ADR-015, `agents/tradeoff.py` |
| F2 Budget Reality Check | done | ADR-016, `reality.py`; UI card, API `reality`, CLI `plan` (stderr) |
| F3 Budget ledger | done (backend, CLI, API, UI card) | ADR-017, `ledger.py`, `tests/test_ledger.py` |
| F4 "Why?" panel | done | Vendors tab button; Playwright-tested |
| F6 Locale layer | partial: pack v1 and the confirmation card done (chat intake, ADR-022); Hinglish open | ADR-019, ADR-022 |
| F5 Scheduler | partial: multi-organizer done; zones, working hours, multi-day open | ADR-020 |
| F7 Rule packs | partial: dietary checklists done (all unreviewed); traditions and accessibility open | ADR-021 |
| F24 Assistant | v1 done: RAG, tools, RBAC, proposals, cache/router, traces, prompt lock; behavioral evals never run on a real model | ADR-023 |
| Follow-through jobs | built: N quotes by a deadline across vendors, claims the user confirms, nudges, expiry, API, UI, CLI, worker; no outside notifications | ADR-044, `jobs.py`, `claims.py`, `worker.py`, `docs/JOBS_DESIGN.md` |
| F11 Vendor conversations (email + WhatsApp) | built: draft, user approves exact text, agent sends (SMTP / WAHA), polls replies (IMAP / WAHA), inbox, analysis, follow-ups, limits, optional standing permission for automatic follow-up questions (ADR-043); **not run against real accounts** | ADR-042, `channels.py`, `conversations.py`, `outreach.py` |
| F10 Vendor score | built (backend, API, CLI, Budget-tab card); thresholds unvalidated; needs 3 current quotes to score price | ADR-040, `vendorscore.py` |
| F9 Observation ledger + quote intelligence | done for local use: store, report, quote-driven pricing, Quotes card, "confirmed by vendor" label, re-price repair | ADR-006, ADR-025/026/027 |
| F8 | already built | |
| Travel: flight fares/schedules (fli) | done, offline-tested plus one live run; multi-city composed from per-leg searches | ADR-028, `travel.py` |
| Web deals agent (search, read, extract, rank) | built; live-tested for caterer/venue/photographer in Pune; venue coverage thin; leads only | ADR-031, `deals.py`, `web_access.py`, `websearch.py` |
| Multi-city trip search | built, live-tested | ADR-030, `tripsearch.py` |
| Venue discovery (Overpass) | built and fake-server tested; **not verified live** (public server was at capacity) | ADR-028, `discovery.py` |
| Weather (OpenWeather) + `.env` loader | built; **live call returned 401** (key probably still activating); fake-server tested | ADR-029, `weather.py`, `env.py` |
| F10-F23 | not started | `ROADMAP.md` |

## Known limitations (what the roadmap fixes)

Scheduler is single-organizer/local-time (F5, ADR-004) · parser is English/INR only (F6, ADR-005) ·
vendor data is synthetic and confirmations are user-asserted (F9-F11, ADR-006) · web server is local single-user (F23, ADR-014).
Budget rebalancing is no longer manual: F1 proposes verified options, the user applies one.

## Next step (start here)

00. **Run the whole live check**: add `NVIDIA_API_KEY` and/or `OPENROUTER_API_KEY` (and later `GOOGLE_PLACES_API_KEY`, `BRAVE_SEARCH_API_KEY`) to `MultiAgentPlanning/.env`, then
   `python -m eventmate.smoke` (free) and `python -m eventmate.smoke --llm` (a few credits). Then, once, the assistant's behavioral evals:
   `python -m eventmate.assistant.evals --behavioral --update-lock` (spends credits; until then `prompts/assistant.lock.json` says `not_run`).
   Edit `eventmate/data/assistant_policy.json` (competitor and phrase deny lists; the shipped names are examples).

0. **Add the facts the AI cannot collect yet**: sattvic/no-onion-garlic and allergen values (allergens as hard checks, kept separate from
   diet preferences) and an accessibility pack. Schema change first, then `IntakeDraft`, the review-card toggles and tests.

Phase 2 is done except the open parts listed in the table (IANA zones/working hours/multi-day, Hinglish, tradition packs).
Those need either a design that is not blocked by missing data or outside reviewers, so move on:

0b. **Verify the live integrations** once possible: run `python -m eventmate weather Pune <date within 5 days>` (a 401 means the key is not active yet or the value
   in `.env` is not the OpenWeather key) and `python -m eventmate discover Pune` (retry later or set `EVENTMATE_OVERPASS_URL`). Then, if wanted: flights for out-of-town guests
   in plans, weather into F18 re-timing, Foursquare/Duffel/Amadeus once the user supplies keys or accounts (ADR-028 item 5; add their names to `env.py` ALLOWED when used).
2. Vendor messaging is built behind an approval gate (ADR-042). Voice, recording and any send without an approval stay blocked until the user reports the legal checks done (ADR-018).
2b. **Try it for real**: set the `EVENTMATE_SMTP_*`/`IMAP_*` and/or `EVENTMATE_WAHA_*` names in `.env` (see `.env.example`), save a vendor contact on the Vendor chats tab, draft, approve, then `python -m eventmate inbox poll`. Report what broke: TLS, auth, WAHA chat ids and WhatsApp restrictions were never exercised live.
3. F12/F13 wallet and approval queue can be built before F11, since they send nothing. Clone and read agent-vault (Apache-2.0) first and record the commit here.

## Things to know

- F1: moves are rebalance caps, drop optional service, shift date, raise budget; harm weights in `TradeoffPolicy`.
  `EventRequest.cap_weights` is new; the Validator recomputes allocation; `BudgetPolicy` is shared via `EventMate`.
  Not done: tier/hours downgrades (no data), combined moves.
- F2: floor = cheapest eligible vendor per service, labelled "Estimate, not a quote". The CLI printed nothing for it until this
  session (gap found by running the CLI; fixed with a test).
- F3: ledger files live in `<plan_dir>/ledger/`. They are **not** covered by the plan store's SHA/HMAC (ADR-017 consequence).
  The ledger does not feed the Validator or the Replanner. A replan that drops a paid category shows the money as `orphaned_paid`.
  Refunds above the amount paid are refused. No payment is ever initiated.
- F5: `organizers` only raises the organizer capacity; the effect on the schedule is small (ADR-020). No UI control, set via request text.
- F7: packs are data in `data/rule_packs.json`; a test fails if any pack claims `reviewed` (flip deliberately, with a named reviewer).
- Process lesson: F2 was built in an earlier session but never logged, so the handoff was stale and the test count wrong (418 vs 430).
  The memory-files rule exists for this; log in the same change.

## Open questions for the user

Answered 2026-10-30 (ADR-018): vendor city/language unspecified (voice stays deferred), no managed Postgres (SQLite first), the user checks the legal items.
Still open:
1. Which Google account/project will own OAuth app verification (F12)? (User said: keep for later.)
2. When the user finishes the legal checks, tell me which of F11/voice/recording are cleared.
3. Who can review tradition packs (rituals, timing, invitation wording)? Until someone is named, only the unreviewed dietary checklists ship (ADR-021).

## References (ADR-013)

Existing `../_refs/`: haystack (Apache-2.0, 4b5dd70), langfuse (MIT core, 5cb2047) and dify (f36f75a4) now used as pattern sources per ADR-024 (user owns the licence question; no source copied), portkey gateway (MIT, 669825c), guardrails-ai (Apache-2.0, 06d0ff2), all read 2026-10-30 for ADR-023/024; Agent-Oriented-Planning, MC-Planner, async_planning, Awesome-LLM-Planning, openmuse (CopilotKit, MIT, commit a4944f4, read 2026-10-30, ideas only: strict choice panels, approvals bound to reviewed content, per-feature data-disclosure notes, durable task leases and receipts for F15/F18).
Roadmap-note repos were checked on 2026-10-30 through the GitHub API **metadata only**; none cloned and no code read yet:

| Repo | Licence | Stars | Last push | Verdict |
|---|---|---|---|---|
| google/mcp | Apache-2.0 | 4669 | 2026-08-17 | usable reference for Workspace patterns (F12/F20) |
| CrimsonWorks-Inc/agent-vault | Apache-2.0 | 0 | 2026-10-09 | licence ok, unproven; read before F12, reimplement rather than depend |
| kh0pper/google-workspace-mcp | MIT | 0 | 2026-10-28 | unproven; reference only |
| lukanlabs/lukan | MIT | 35 | 2026-08-22 | Rust; reference only |
| Lev0n82/AutonomousBrowserAutomation | MIT | 0 | 2026-10-19 | unproven; reference for F19 only |
| SophiaYifei/travel-agent-planner | MIT | 0 | 2026-04-09 | idea only (budget feasibility, already F2) |
| Aayush99000/RAG-Travel-agent | MIT | 0 | 2026-05-02 | idea only (F22 deferred) |
| VaradhKaushik/AI-Driven-Itinerary-Planner | MIT | 1 | 2024-07-30 | stale |
| alulema/trip-planner, patel-ishaan/hitched, nicola-empower/wedding-planner | **none** | 0-2 | 2026 | **no licence = no copying code**; ideas only (already covered by F1/F3) |

Nearly all have 0-2 stars, so none counts as proven; any borrowing needs a read-through and a licence check first.

## Session log (newest first)

- 2026-10-31: Review fixes (ADR-048): a required access need that is unknown now makes the plan provisional ("Needs verification: required accessibility unknown") in the header, chat, Overview, Community tab, trade-off cards, explanation, CLI validate, API and assistant tool; the test that said unknown never blocks was replaced. Profile cache keys on content. Demo gained a synthetic-data disclosure and the access walkthrough. Branch `cleanup/structure` was pushed for review; nothing is on `main` beyond 82c8d4f.

- 2026-10-31: Reorganised the repository (no behaviour change): modules grouped into subpackages, tests moved to `tests/`, `pyproject.toml`, README rewritten, CONTRIBUTING and Makefile added, the package README folded into `docs/CONFIGURATION.md`. 1512 tests pass unchanged. Module paths in older ADRs were rewritten to the new layout; their prose is otherwise historical.

- 2026-10-31: Implemented the brief-fit prompt: personalized ideas (ADR-045), community events as profile-driven type with hard checks, user-marked conditional items and an Ideas/Community UI (ADR-046), fake-SMTP/IMAP end-to-end email tests, owner digest and optional owner email copy, smoke_email, EMAIL_SETUP/VALIDATION and DEMO docs (ADR-047). Found and fixed: a worker item raised for replies the Mandate answered. Not run: any real mailbox (Task 4 is the user's). Not pushed: all of this is local only; the GitHub repo is at 24e6c48.

- 2026-10-30: Reviewed an outside design and code slice for follow-through jobs and adopted it with nine changes (ADR-044): one permission instead of a second grant system, no assumed quote validity, a stricter amount check, a file lock after real concurrent processes double-claimed a send, aware timestamps, job-end veto, one claimant per outbound action, contact snapshot. Labelled set: 17 of 19 prices found, 0 wrong. Removed README notes the user objected to. The GitHub repo has the earlier code plus the README fix; the jobs work and the automatic follow-up permission are NOT pushed.

- 2026-10-30: Automatic follow-up questions (ADR-043), at the user's request: a per-plan, time-boxed, capped, window-limited standing permission; fixed questions only; hands over on questions, refusals, attachments and stop requests; vendor is told it is automated; per-plan revoke and a global kill switch that also denies queued messages. 27 tests plus a browser test verified by mutation. Mistake caught on the way: my UI text promised the automatic-message disclosure before the code did it; I implemented and tested it. Pushed to GitHub earlier; this change is NOT pushed yet.

- 2026-10-30: Vendor conversation agent (ADR-042), at the user's direction ("the agent should communicate and respond back, like OpenMuse"). Studied OpenMuse (it gates outbound actions behind hash-bound approvals; WhatsApp is not built there) and cloned WAHA (Apache-2.0, c42f68a). Built channels (SMTP/IMAP, WAHA), the approval gate and send claim, reply matching and analysis, limits, a background loop, CLI `inbox`, API, and a Vendor chats tab. 77 new tests, two browser tests, a real SMTP wire check. Bugs the tests found: the content hash treated a missing `kind` differently from "request" (every status change looked like tampering); saved WhatsApp contacts were fed back to the parser without their `+`; replies with equal timestamps were ordered by a random id; "booked on 12 Dec" was not recognised as unavailable (and bare "is booked" is deliberately not, because it can be a confirmation). Not verified live: real mail provider, real IMAP, any WAHA session.

- 2026-10-30: Vendor scores card in the Budget tab (F10 UI): ranked vendors per category, weights, reasons, unscored list. One browser test.

- 2026-10-30: Quote request drafts (ADR-041): email (mailto:) and WhatsApp click-to-chat (wa.me), no API, nothing sent; budget never in the text; Budget-tab card, API, CLI. 37 tests plus a browser test.

- 2026-10-30: Vendor score (ADR-040, F10): price vs median, completeness, evidence, with weights and reasons; missing data is left out and `coverage` says so. 20 tests, API and CLI.

- 2026-10-30: Weather advice (ADR-039): rain backup and heat suggestions from the forecast and the chosen venue's setting, in the Overview weather card. 8 tests.

- 2026-10-30: Saved leads (ADR-038): bookmark an advertised price against a plan from Explore; listed in the Budget tab; never a quote, never prices anything. 17 tests plus a browser test.

- 2026-10-30: Deals search now merges Brave and DuckDuckGo (`MergedSearch`, interleaved, de-duplicated, one provider failing is tolerated). Live: 10 offers from 5 sites (was 4 from 2 with Brave alone).
  Brave quota use is unchanged (one call per query). 1015 tests pass.

- 2026-10-30: Hardening pass (ADR-037) and hotels (ADR-036). Attacked every integration with probes, fuzzers, bandit, a secret-leak scan and the real model; fixed 11 problems (nested-JSON
  crashes, http.client errors, slow-drip reads, hostile result URLs, far-future dates, BOM in .env, assert-based policy checks, CSRF on quota-spending GETs, concurrency cap, router model choice).
  1012 tests pass, pyflakes clean. Live smoke --llm: flights, trip, deals (Brave), weather, places, NVIDIA ping and routing PASS; discover WARN (Overpass busy). Not verified: OpenRouter (no key),
  Overpass, the assistant behavioral evals. Observation: the smoke "llm" ping returned 0 characters from gpt-oss-20b (it spends tokens reasoning); it counts as reachable, not as a quality check.
  The five scraper repos from the user's list were vetted and not adopted (ADR-036). Machine note: another project's Playwright run was saturating the CPU (load ~36), which made one suite run take 4 minutes.

- 2026-10-30: Keys arrived (NVIDIA, Google Places, Brave, Langfuse; OpenRouter still empty). `python -m eventmate.smoke --llm`: flights, trip, deals (via Brave), weather, places
  (10 places, best rated 4.9), NVIDIA ping and free-text routing all PASS; discover WARN (Overpass unreachable). Model calls counted: 2. Added `LANGFUSE_BASE_URL` as an alias for
  `LANGFUSE_HOST` (the user's `.env` used it, so export would have stayed silently off). Langfuse export is now ON for the assistant's masked traces (ADR-024). Observation: Brave returned
  fewer pages than DuckDuckGo for the same deals query (4 offers/2 sites vs 10/5); merging both providers' results is an open option.

- 2026-10-30: Completed the build end to end for the user's list: models moved to NVIDIA/OpenRouter with a credit guard and Anthropic removed (ADR-032); Google Places adapter and
  Brave-then-DuckDuckGo search fallback (ADR-033); free-text front door and an Explore view (ADR-034); a one-command live smoke test (ADR-035). 966 tests pass, pyflakes clean.
  First live smoke (free sources): flights, trip, deals, weather PASS; discover WARN (Overpass unreachable); places and models SKIP (no keys yet). Not verified live: NVIDIA/OpenRouter
  calls, Places, Brave. Found by building it: the web app created its model client before `.env` loaded; a hostile city name passed the explore schema and only failed later.

- 2026-10-30: Web deals agent (ADR-031): safe fetcher (SSRF, robots, pinned IP, caps), DuckDuckGo/Brave search, regex price extraction, cautious ranking, API `/api/deals`,
  CLI `deals`, 72 new tests (874 total). Live runs worked; the first ranking put add-ons on top, so uncorroborated lows are now flagged and ranked last; venue coverage is thin.
  The user pasted a partial Google key in chat; it was not stored. Next: Google Places adapter (ratings, reviews, price level) once the full key is in `.env`; a Deals UI tab.

- 2026-10-30: Trip search (ADR-030): `tripsearch.py`, schemas, API `/api/travel/trip`, tool-call logging. 28 tests; live DEL/BOM/GOI run worked (12 searches, 4 s).
  Open: UI form, LLM intake for trips, reviews/price sources for venues and catering (needs the user's go-ahead on sources and a Google Places key), CLI command.

- 2026-10-30: Venue discovery (Overpass, ADR-028) and weather (OpenWeather, ADR-029) with a `.env` loader. 74 new tests (775 total, pyflakes clean).
  Live checks: flights worked (see below); Overpass public server "at capacity" and mirrors timed out (unverified); OpenWeather answered 401 with the user's key.
  Mistakes: (1) my first inspection of the user's `.env` tried to mask values but the file had no `NAME=` so the key was printed in full into the session
  output (told the user, suggested rotating it); (2) a 503 "not configured" from the weather card broke four browser tests via console errors, fixed with a status endpoint;
  (3) one test of mine used the wrong UTC hour. The user's `.env` was reformatted to `OPENWEATHER_API_KEY=...`, mode 600; `.env.example` added.

- 2026-10-30: Flights (ADR-028), at the user's request. Cloned fli (MIT, 881aee5) to `_refs/`, installed it in its own venv and in `.venv`, reproduced the
  multi-city failure (`SearchUnsupportedError`, upstream #223, by design), and built `travel.py` with multi-city composed from one-way searches.
  Live run 2026-10-30: DEL-BOM 3 options from INR 6,688; DEL-BOM-GOI-DEL total INR 20,524 (cheapest per leg, not a multi-city fare).
  701 tests pass, pyflakes clean. Found by a test: validation lived only in the fli adapter; moved it into the service. Not verified: other routes,
  children/premium cabins, behaviour under Google rate limits, fli's terms of use (user's call).

- 2026-10-30: Finished F9 part 3 (ADR-027): `quote_status`, API `/api/plans/<id>/quotes`, Budget-tab Quotes card with record form, stale-price banner and
  Re-price button, "Confirmed by vendor" badge. 6 new tests plus an extended Chromium journey; 680 pass, pyflakes clean. Two e2e failures were my
  test errors (form clears after save; re-pricing adds a version) and one real gap, the UI hid the API's validation detail, now shown.

- 2026-10-30: Built F9 part 2 (ADR-026): `VendorRepository.price` is the single price path at all six former `cost_for` sites; valid quotes
  reprice plans, expired ones fail `quote_valid` with a re-quote repair, public listings never price. 10 new tests; 674 pass, pyflakes clean.
  Two of my first test failures were wrong expectations (a cheaper quote changes ranking; the service resets the clock from `today`).
  Not verified: UI, real quotes, multi-user clock behaviour.

- 2026-10-30: Built F9 part 1 (ADR-025): `QuoteObservation` schema, `ObservationStore` (append-only JSONL), deterministic quote report
  (hidden costs, unquantified costs, missing/excluded checklist items, valid/expiring/expired), comparison, API `/api/observations/*`,
  CLI `quotes`, `data/quote_checklists.json` (unreviewed). 26 new tests (hostile text, control characters, boundaries, corrupt file,
  CSRF); 664 total pass, pyflakes clean. Not verified: any real quote data, UI, and **no plan uses these quotes yet**. Two failures
  were my own test errors (a hostile string over the 80-char cap; the app's validation status is 422, not 400). Recording is API-only.

- 2026-10-30: Added the langfuse and dify patterns (ADR-024), at the user's direction (they own the licence question; we copied no source):
  opt-in masked trace and score export to Langfuse, thumbs feedback, approved answers (annotation reply), input moderation with a preset reply,
  retrieval score threshold, suggested questions. 15 new tests plus a longer Chromium test; 638 total pass. Not verified: the Langfuse export
  against a real server (only a fake endpoint), and approved-answer matching beyond word overlap.
- 2026-10-30: Built the assistant (ADR-023, F24): `eventmate/assistant/` (identity, guard, knowledge/RAG, tools, approvals, router, cache, trace, agent, evals),
  `prompts/assistant.system.v1.md` + lock, `data/knowledge/`, `data/assistant_policy.json`, API `/api/assistant/*`, Ask tab. Borrowed ideas from
  guardrails-ai (re-ask, deny lists), portkey (retry, fallback, cost cap), haystack (recall/MRR evals); read langfuse and dify, built nothing from them.
  79 assistant tests with scripted hostile models + a Chromium test; 623 total pass, pyflakes clean. Not verified: any real model run, real
  logins or multi-user roles, cost numbers beyond estimates. Two failed tests were my own test errors (a wrong fence expectation, an exhausted script).
- 2026-10-30: Built conversational intake (ADR-022): `intake.py`, `prompts/intake.extract.v1.md`, API `/api/intake` and `/api/intake/confirm`,
  chat panel with a one-sentence editable review card and one-tap choices; classic form kept in a collapsed section. 49 new tests
  (injection, malformed model output, ambiguity, approval receipt) and a Chromium test. 543 tests pass, pyflakes clean.
  Not verified: a live LLM run (none was called; tests use a fake client), Hinglish input, mobile layout. A repo test banned the data-field
  name `budget_total` in prompts, so the prompt uses `budget_inr` and the code maps it. Cloned and read openmuse for ideas.
- 2026-10-30: Built F7 v1 (ADR-021): `rules.py`, `data/rule_packs.json` (5 diets, all unreviewed), API, Overview card, 14 tests + e2e step.
  Chose not to write tradition/ritual content because I cannot verify it. 493 tests pass, pyflakes clean.
- 2026-10-30: Built F5 part 1 (ADR-020): `EventRequest.organizers`, parser, scheduler capacity, Validator policy check, 11 tests
  (old requests still load, tamper test, replan keeps it). Measured effect and recorded it as small. 479 tests pass, pyflakes clean.
- 2026-10-30: Recorded the user's answers (ADR-018). Built F4 (Why? panel, e2e-tested). Probed the parser and found `₹ 2L` => INR 2
  (silent wrong budget); built locale pack v1 (ADR-019): lakh letter, Indic digits, day-first numeric dates with ambiguity as error.
  468 tests pass. Mishap: my new test constant `BASE` shadowed an existing one and broke 7 older tests until renamed `LOC_BASE`.
  Not verified: Hinglish, non-INR currencies, real vendor-language input.
- 2026-10-30: Found F2 already built but unlogged; wrote ADR-016. Gap fixed: CLI `plan` now prints the reality check on stderr.
  Built F3 (ADR-017): `Ledger`, schemas, `EventMate.ledger_*`, CLI `ledger`, API GET/POST `/api/plans/<id>/ledger`, Budget-tab card,
  Playwright step; fixed a money-hiding gap (`orphaned_paid`) with a test. Removed two pyflakes leftovers from F1.
  Vetted roadmap repos via GitHub metadata (table above). 448 tests pass. Failure worth noting: my first e2e wait used string
  `wait_for_function`, which the strict CSP blocks; replaced with a selector wait.
- 2026-10-30: Built F1 (ADR-015): `agents/tradeoff.py`, `CapWeight`/`TradeoffOption`/`TradeoffReport` schemas, Manager dry-run
  split, Validator allocation re-derivation, CLI/API/UI, 27 new tests. Ran the CLI end to end; fixed one bug it exposed.
- 2026-10-30: Reviewed codebase and rules; ran tests (387 pass); wrote ADR-001..014, ROADMAP triage,
  this handoff, the "Memory files" rule and `test_memory_files.py`. No product code changed.
