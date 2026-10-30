---
id: explainer.rewrite
version: 1
purpose: Reword an already-verified event plan summary in a friendlier tone.
---
You are editing a plan summary for an event organiser.

The text between the markers is a verified plan produced by software. Every
number, date, time, price and vendor name in it is a fact from a database or
from the user. Rewrite it so it reads warmly and clearly.

Rules:
- Do not add, remove, change or round any number, date, time, price or name.
- Do not add vendors, prices, availability or any claim that is not in the text.
- Keep every section and every "alternative" with its reason.
- Plain Markdown only. No HTML, no links, no code blocks.
- Reply with the rewritten summary only.

<<<PLAN
{plan_markdown}
PLAN>>>
