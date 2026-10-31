# What each model stage is sent

Models are optional (`EVENTMATE_ENABLE_LLM=0` turns them off). Contact details are replaced by placeholders (`<EMAIL_1>`, `<PHONE_1>`) before any prompt is built; the map stays in the process and is never logged or sent.

| Stage | Input | Caps | Masked before sending |
|---|---|---|---|
| Intake | the user's message, the reviewed draft, today's date, subtype names | message 2000 chars; up to 600 tokens out | emails, phone numbers (placeholders) |
| Assistant | the question, a tool catalogue, tool results, the last 4 question/answer pairs (fenced, untrusted) | question 1000 chars; 4 steps, 700 tokens out | emails, phone numbers; tool results also mask PAN, Aadhaar, card numbers |
| Ideas | eight facts: event type, guest count, budget band, diets, city, venue setting, up to 5 preferences, subtype | 900 tokens out | preferences pass through the same placeholder step |
| Explanation | the verified plan summary (vendor names, numbers) | 3000 tokens out | no raw request and no guest names are in it |

Not sent to any model: the budget total or ledger (except as numbers a tool returns to the assistant), vendor reply text, discovered contact text, secrets.
Optional Langfuse export, if configured, sends masked questions and answers off the machine.
