# EventMate

A multi-agent event planner built on the MultiAgentPlanning repository. It turns a natural-language
request into a **validated, explainable, replannable** itinerary. The base PDDL/LangGraph pipeline in
`src/llm_plan` is untouched; EventMate reuses its toolchain (Fast Downward, VAL) and adds a typed
agent layer around it.

```
"Plan a 120-person engagement party in Pune on December 12 under INR 200,000. Vegetarian catering,
 indoor venue, photographer, and reminders for invitations and vendor confirmation."
        |
   Manager ── parse ─> EventRequest ── decompose ─> TaskGraph (solvable / complete / non-redundant / necessary)
        |
   Guest ── RSVPs, diets ─┐
   Budget ── allocation ──┤ -> Vendor ── shortlist per category (every hard constraint, every reason)
                          |
   Planner ── best vendor set ── Scheduling ── CP-SAT schedule ── PDDL (Fast Downward + VAL)
        |
   Validator ── re-derives every fact from the database; plan cannot exist unless all checks pass
        |
   Plan store ── immutable, hash/HMAC-checked versions     Explainer ── text with reasons + alternatives
        |
   Replanner ── cancellation / budget / guest count / date  ->  new version + added/removed/changed/preserved diff
```


## Talking to vendors

EventMate can write to your vendors and read their replies (ADR-042). Every message is a proposal you approve (the exact text, bound to a hash); the agent then sends it through **your own** mailbox
(SMTP/IMAP) or a self-hosted [WAHA](https://github.com/devlikeapro/waha) WhatsApp gateway, and shows what comes back in the **Vendor chats** tab. Replies are shown as the vendor's own words with the
amounts and questions quoted; nothing becomes a quote until you record it. Settings are in `.env.example`; see `docs/EMAIL_SETUP.md`.
`python -m eventmate inbox show|poll|approve|deny` does the same from the terminal.
Optionally you can give the agent a standing permission to ask routine follow-up questions on its own (ADR-043): fixed questions only, time-boxed and capped, with a kill switch.

**Email status: tested with fake SMTP/IMAP only; not yet validated against a real mailbox.** (`docs/EMAIL_VALIDATION.md` is the place for that record.)

Safety wording, stated precisely: the fixed request templates and the automatic follow-up templates never contain your budget or a price. Free-text follow-ups you write yourself are cleaned and
length-limited but not filtered for numbers: what you type is what is sent. Sends use claim protection (a draft is claimed before it is sent, by a file lock across processes) and conservative
handling of an unknown outcome (a send whose result cannot be known is never retried automatically); this reduces the chance of a duplicate and does not make one impossible.

## How this meets the brief

The brief: a GenAI-powered planning assistant for weddings, birthdays and community events, with budgets, timelines, vendor checklists and personalized ideas.

| Brief item | Where it lives | Notes |
|---|---|---|
| Budgets | `agents/budget.py`, `reality.py`, ledger, trade-offs | Default split is normalized weights: about 36% venue, 41% caterer, 12% photographer of the budget left after a 10% contingency reserve (weights 3500/4000/1200 basis points over their sum of 8700). A community event uses its own profile's shares; you can override any split |
| Timelines | `agents/scheduler.py`, `cpsat.py`, PDDL cross-check | CP-SAT schedule with lead times, buffers and shared resources; community programme activities are scheduled by the same solver |
| Vendor checklists | `data/quote_checklists.json`, quote ledger, vendor score, vendor chats | The checklist is what a quote request asks the vendor; the ledger shows what is missing |
| Personalized ideas | `ideas.py`, Ideas tab | Suggestions from verified plan facts; templates work with the model off; every model idea is guarded; pin or dismiss |
| Weddings, birthdays | `EventType` and the planner | Unchanged by the community work (regression-tested) |
| Community events | `community.py`, `data/community_profiles.json`, Community tab | Hard checks, conditional items only you can mark, and suggestions are kept apart |
| Where GenAI is used | intake reading, ideas, assistant routing and answers, explanation rewording | Every model output is guarded and degrades to deterministic text |
| Where GenAI is deliberately not used | plan validity, prices, availability, capacity, cultural or legal claims, quote recording | Code and data decide these; a model may only phrase or propose |

## Quick start

```bash
source .venv/bin/activate                      # the repo's existing venv
pip install -r eventmate/requirements.txt      # pydantic, ortools, flask, pytest

python -m eventmate plan "Plan a 120-person engagement party in Pune on December 12 under INR 200,000. \
Vegetarian catering, indoor venue, photographer, and reminders for invitations and vendor confirmation."
python -m eventmate confirm <plan_id> venue                       # you confirmed this booking
python -m eventmate replan <plan_id> --cancel-vendor cat-pune-rasoi
python -m eventmate replan <plan_id> --date 2026-12-19 --override-confirmed ven-pune-orchid
python -m eventmate tradeoffs "<request that fails on budget>" --apply 1   # verified options, then build the chosen one
python -m eventmate show <plan_id> ; python -m eventmate diff <plan_id> 1 3 ; python -m eventmate validate <plan_id>

python -m eventmate.web                         # UI on http://127.0.0.1:8765
python -m pytest eventmate/tests                # 1499 passed on Python 3.14.8 with the solvers and Chromium present (browser tests skip without Chromium)
```

Fast Downward and VAL are built under `solvers/` (gitignored) by the base repo's `bootstrap.py`, or:
Fast Downward: download release 24.06.1 and `python build.py release`; VAL: `cmake -DCMAKE_POLICY_VERSION_MINIMUM=3.5`
then `make Validate`. Without them, planning stops with an explicit "symbolic check unavailable" failure unless you
pass `--allow-no-symbolic` (the plan then carries a warning).

## Guarantees (each is enforced and tested)

- No invented facts: availability, prices, capacities, diets and booking status come from the seed database, the
  user, or a tool. The default build uses no LLM. The optional LLM only rewords a verified summary and is rejected
  if it changes any number, drops a vendor name, or adds markup/links.
- A plan is never shown unless every hard constraint passed an *independent* re-check (`PlanVersion` cannot be
  constructed otherwise).
- Every failure names the violated constraint and a concrete repair (repair dates come from the database or a solver search).
- Every recommendation lists at least one alternative (or says why none exists).
- Every replan writes a new immutable version and a diff; nothing is overwritten. Confirmed bookings are preserved
  unless the vendor cancelled or you explicitly release them; a conflict returns `needs_decision` and saves nothing.
- Every material decision is logged (JSONL) with alternatives, constraints checked, justification, confidence and source.
  Also logged: tool calls, LLM traces (hashes only by default), validation failures.

## Configuration (environment variables)

| Variable | Purpose |
|---|---|
| `EVENTMATE_SIGNING_KEY` | HMAC key (>=16 bytes) that makes stored plans tamper-evident even against someone who can write files |
| `EVENTMATE_WEB_TOKEN` | Bearer token required for the API (mandatory for non-loopback binds) |
| `EVENTMATE_FD`, `EVENTMATE_VAL` | Paths to Fast Downward / VAL if not in the default locations |
| `NVIDIA_API_KEY`, `OPENROUTER_API_KEY` (`EVENTMATE_ENABLE_LLM=0` turns models off) | Models: NVIDIA for heavy work, OpenRouter free models for light work and as backup; no SDK needed |

Secrets are only read from the environment and never written to disk or logs.

## Layout

`schemas.py` typed contracts · `agents/` the nine agents · `cpsat.py` OR-Tools scheduling · `symbolic.py` PDDL ·
`plan_store.py` immutable versions · `logs.py`/`jsonl.py`/`decision_logger.py` JSONL channels · `llm.py` prompt store + guard ·
`service.py` facade · `web/` Flask API + UI · `data/` synthetic seed data · `../prompts/` versioned prompts · `tests/`.

## Keys and optional integrations

Secrets live in `MultiAgentPlanning/.env` (git-ignored; copy `.env.example`, then `chmod 600 .env`). Format: `NAME=value`. Only the names in
`eventmate/env.py` are read, and a value already set in your shell wins. Nothing prints or logs a value.

| Feature | Needs | Command / endpoint |
|---|---|---|
| Weather (OpenWeather) | `OPENWEATHER_API_KEY` | `python -m eventmate weather CITY YYYY-MM-DD`, Overview card |
| Flight fares and schedules (fli, Google Flights) | `pip install ../_refs/fli` (no key) | `flights`, `multicity`, `/api/travel/*` |
| Free-text search (Explore) | `NVIDIA_API_KEY` and/or `OPENROUTER_API_KEY` | `/api/explore`, Explore view (forms work without a key) |
| Ratings and reviews (Google Places) | `GOOGLE_PLACES_API_KEY` | `/api/places`, Explore view |
| Deals agent: searches the web, reads pages, ranks advertised prices | none (DuckDuckGo); optional `BRAVE_SEARCH_API_KEY` | `deals`, `/api/deals` |
| Venue discovery (OpenStreetMap / Overpass) | none; optional `EVENTMATE_OVERPASS_URL` | `discover`, `/api/discover/venues` |

Flights never book or pay. Multi-city is built from separate one-way searches because Google does not serve multi-city results to fli. Discovery
results are candidates, not vendor-database facts. Forecasts exist for about five days ahead only.

Deals are leads: advertised prices from public pages, flagged when unusually low or very wide, never quotes. The agent obeys robots.txt and cannot reach private addresses.

Check everything at once: `python -m eventmate.smoke` (free sources only) or `python -m eventmate.smoke --llm` (also pings the model providers; a few credits). Tests never call a keyed service.
