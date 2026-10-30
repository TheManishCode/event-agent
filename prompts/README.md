# Prompts

Versioned Markdown prompts for EventMate agents live here, one file per prompt,
named `<agent>.<purpose>.v<N>.md`. A prompt is never edited in place after it
is used in a run: add `v<N+1>` instead, so logged LLM traces stay reproducible.

Rules (see `AI_CODING_RULES.md`):

- Prompts carry wording and output format only. No business logic, thresholds,
  prices, availability, capacities or booking status.
- Prompts only ever receive data fenced between markers, and user text has those markers stripped.
- `intake.extract.v1.md` (ADR-022): reads the organiser's words into a fixed JSON shape. The reply is untrusted data: it is schema-validated, never executed, and a human approves the result before anything is planned.
- `assistant.system.v1.md` (ADR-023): the assistant's rules and reply format. Pinned by `assistant.lock.json`; a test fails if it changes without `python -m eventmate.assistant.evals --update-lock`.

