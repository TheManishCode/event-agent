# event-agent

EventMate: a constraint-verified event copilot for Indian weddings, birthdays and community events. It turns a brief into a plan that is
checked against budget, capacity, date, dietary and vendor constraints, offers priced trade-offs when they conflict, replans without breaking
confirmed bookings, and talks to vendors for you: it writes quote requests, sends them after you approve the exact text, reads the replies and
shows what came back. Nothing a vendor says becomes a quote until you record it.

What is in here: the `eventmate/` package (agents, scheduler, validator, quote ledger and vendor score, web UI, CLI, vendor conversations over
email and WhatsApp), `prompts/` (wording only, versioned), and `docs/` (decisions, roadmap, handoff). Start with `docs/HANDOFF.md`, then
`AI_CODING_RULES.md` and `eventmate/README.md`.

## Run it

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r eventmate/requirements.txt
cp .env.example .env && chmod 600 .env     # keys and mailbox settings; .env is git-ignored
python -m eventmate.web                    # UI on http://127.0.0.1:8765
python -m pytest eventmate/tests           # the test suite
```
