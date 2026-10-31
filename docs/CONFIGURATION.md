# Configuration

## Environment variables

| Variable | Purpose |
|---|---|
| `EVENTMATE_SIGNING_KEY` | HMAC key (>=16 bytes) that makes stored plans tamper-evident even against someone who can write files |
| `EVENTMATE_WEB_TOKEN` | Bearer token required for the API (mandatory for non-loopback binds) |
| `EVENTMATE_FD`, `EVENTMATE_VAL` | Paths to Fast Downward / VAL if not in the default locations |
| `NVIDIA_API_KEY`, `OPENROUTER_API_KEY` (`EVENTMATE_ENABLE_LLM=0` turns models off) | Models: NVIDIA for heavy work, OpenRouter free models for light work and as backup; no SDK needed |

Secrets are only read from the environment and never written to disk or logs.

## Keys and optional integrations

Secrets live in `.env` at the repository root (git-ignored; copy `.env.example`, then `chmod 600 .env`). Format: `NAME=value`. Only the names in
`eventmate/env.py` are read, and a value already set in your shell wins. Nothing prints or logs a value.

| Feature | Needs | Command / endpoint |
|---|---|---|
| Weather (OpenWeather) | `OPENWEATHER_API_KEY` | `python -m eventmate weather CITY YYYY-MM-DD`, Overview card |
| Flight fares and schedules (fli, Google Flights) | `pip install flights` (no key) | `flights`, `multicity`, `/api/travel/*` |
| Free-text search (Explore) | `NVIDIA_API_KEY` and/or `OPENROUTER_API_KEY` | `/api/explore`, Explore view (forms work without a key) |
| Ratings and reviews (Google Places) | `GOOGLE_PLACES_API_KEY` | `/api/places`, Explore view |
| Deals agent: searches the web, reads pages, ranks advertised prices | none (DuckDuckGo); optional `BRAVE_SEARCH_API_KEY` | `deals`, `/api/deals` |
| Venue discovery (OpenStreetMap / Overpass) | none; optional `EVENTMATE_OVERPASS_URL` | `discover`, `/api/discover/venues` |

Flights never book or pay. Multi-city is built from separate one-way searches because Google does not serve multi-city results to fli. Discovery
results are candidates, not vendor-database facts. Forecasts exist for about five days ahead only.

Deals are leads: advertised prices from public pages, flagged when unusually low or very wide, never quotes. The agent obeys robots.txt and cannot reach private addresses.

Check everything at once: `python -m eventmate.smoke` (free sources only) or `python -m eventmate.smoke --llm` (also pings the model providers; a few credits). Tests never call a keyed service.

Vendor messaging settings are in [EMAIL_SETUP.md](EMAIL_SETUP.md). Community profiles and idea templates are data files under `eventmate/data/`; set `EVENTMATE_COMMUNITY_PROFILES` to use another profiles file.

## Finding vendors
| Source | Covers | Needs |
|---|---|---|
| OpenStreetMap (Overpass) | venues only | nothing; optional `EVENTMATE_OVERPASS_URL` |
| Google Places | venues, caterers, photographers | `GOOGLE_PLACES_API_KEY`. A shortlist search also requests `places.websiteUri` and `places.id`, which Google bills at a higher tier than the base fields; this has not been run against a live key |

Contact addresses are read only from a business's own website (the listing's, or one you type) and at most one same-domain contact page, honouring robots.txt. Found addresses are never used until you confirm them. Business addresses and the do-not-contact list are stored in the plan directory (`sourcing` data: `externals/`, `shortlist/`, `suppression/`, `unmatched/`) with no retention or deletion rules yet.

