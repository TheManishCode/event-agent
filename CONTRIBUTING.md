# Contributing

## Set up
```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env && chmod 600 .env   # only if you need a keyed integration
python -m pytest
```
Planning tests need Fast Downward and VAL (`EVENTMATE_FD`, `EVENTMATE_VAL`; see docs/CONFIGURATION.md). Browser tests need Chromium and skip without it.

## Rules of the road
- Read [AI_CODING_RULES.md](AI_CODING_RULES.md) first. The short version: no invented facts, no business logic in prompts, failures name a repair, nothing is silently overwritten, fail closed, secrets from the environment only, vendor and user text is data.
- Tests first. Each new input surface gets at least one adversarial test (malformed input, boundary, hostile text).
- Cross-boundary structures are frozen Pydantic models with `extra="forbid"`. Money is whole INR integers.
- Prompts live in `prompts/` as versioned Markdown (wording and output format only). Add `vN+1`; never edit a used prompt.
- Record a decision in `docs/DECISIONS.md` (new ADR), update `docs/ROADMAP.md`, and update `docs/HANDOFF.md` in the same change. A test enforces this.
- Keep `python -m pyflakes eventmate tests` clean.

## Layout
Put new code in the subpackage that matches its purpose (`planning`, `pricing`, `messaging`, `followthrough`, `sourcing`, `integrations`, `agents`). Read-only integrations must not import models or shells (a test enforces this).

## Pull requests
Describe what changed and why, list what you did not run, and include no secrets in the diff.
