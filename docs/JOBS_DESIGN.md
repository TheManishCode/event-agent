---
title: "EventMate: from planner to follow-through agent"
subtitle: "Design and status for the follow-through jobs (ADR-044)"
---

## 0. Status in this repository (living document; decisions in ADR-044)

| Piece | State |
|---|---|
| `jobs.py`: Job model, SQLite store (private file), legal transitions, version check (stale writes refused), event history, time-zone-aware timestamps only | Built, tested |
| `claims.py`: price claims from replies; the amount must be written out as such (a multiplier counts only when the vendor wrote it); the vendor's stated validity is captured only when written | Built, tested (32-reply English and Hinglish labelled set: 17 of 19 recordable prices found, 0 wrong proposals) |
| `worker.py`: `tick()`, start job from saved vendor contacts (recipient snapshotted per job), approve requests, confirm (the user states the validity) or reject a claim, nudges, expiry, digest text; CLI `python -m eventmate.worker --once/--every` and `eventmate jobs ...` | Built, tested |
| One permission: automatic nudges go through the standing permission of ADR-043 (kill switch, window, caps, disclosure); there is no second grant system | Built, tested |
| One claimant per outbound action (`action_key`), a dispatch-time veto for automatic messages of an ended job, a file lock so the web process and a separate worker cannot both claim a send | Built, tested (real concurrent processes) |
| API (`/api/plans/<id>/jobs`, `/api/jobs/...`), Quote jobs panel in the Vendor chats tab, tab badge, CLI | Built, tested (Chromium) |
| Real mailbox or WAHA, real vendor replies, PDF quotes, notification sending, webhooks, LLM extraction, load | Not built / not tested |
| Next-candidate drafting when a vendor is unavailable, automatic replan after a confirmed quote | Not built: the job raises an item for you; re-pricing the plan stays a manual click |

## 1. Goal and honest scope

Today EventMate turns a brief into a verified plan and, once you click, sends approved vendor messages and reads replies. The aim here is an agent that **keeps a job moving across days** (send, wait, read, decide, nudge, ask you, finish) while staying inside limits you set.

This is an independent design for your own system, built from your repo and ordinary engineering patterns. Nothing here is measured. Do not claim any efficiency level until the evals in section 9 pass on your own data. No code in the repo was changed.

## 2. What you already have (read in source at 853186fb)

| Capability | Where | Note |
|---|---|---|
| Hash-bound approval of exact message text, with expiry | `conversations.approve`, `OutreachDraft.content_hash`, `expires_at` | Keep as is |
| Send claim before send, 5 min lease, `outcome_unknown` on crash or timeout, never auto-retried | `ConversationService.dispatch/_send_one` | This is already an outbox-style guard. Extend, don't replace |
| Send limits per day, per vendor, WhatsApp gap | `SendPolicy` | Keep |
| Reply polling, thread matching (email headers, then sender) | `poll`, `_match` | Runs only when called |
| Rule-based reply reading: amounts, available/unavailable, questions, checklist | `analyze_reply` -> `ReplyAnalysis` | Quotes the vendor, records nothing |
| Quote ledger with expiry, confidence, evidence ref, comparison and reports | `observations.py`, `QuoteObservation` | The right sink for reply claims |
| Vendor score, trade-off options, replan with diff, validator | `vendorscore.py`, `agents/tradeoff.py`, `replanner.py`, `validator.py` | The decision engine the loop calls |
| Approval queue for assistant proposals | `assistant/approvals.py` | Reuse the pattern for job actions |
| Append-only JSONL stores with transition checks | `jsonl.py`, `OutreachStore.transition` | Fine for one local process |

What is **absent** (searched the non-test code): any timer, worker, daemon or event loop (the only `threading` use is locks); a job or goal object; any path from a vendor reply to a ledger record or replan without manual entry; any user notification outside the UI; scoped standing permissions.

## 3. Target architecture

```
 timers + inbound events ──> JobWorker.tick() ──> Job (state, next_action, wake_at)
                                   │
        ┌──────────────────────────┼───────────────────────────┐
   observe                      decide                       act
 poll channels            rules first, LLM only        propose/send via existing
 read new replies         for drafting/classifying     ConversationService
 check deadlines          within a Policy              (hash approval or Grant)
        │                          │                           │
        └──── ClaimExtractor ──> user confirm ──> ledger ──> replan/tradeoff proposal
                                   │
                              Notifier (WhatsApp/email digest) <── user decisions resume job
```

Principles: the **state machine owns the goal**, the LLM never owns state; every external effect goes through the existing approval and send path; every tick is bounded and ends in a named state.

## 4. Data model (new files, same Pydantic style)

`eventmate/jobs.py` (built), stored in SQLite (`jobs.db`) from day one. The code differs from the sketch below in small ways: vendor progress, claims and open user items live inside the Job, and the Grant is embedded in the Job. JSONL is fine for logs, but jobs need atomic claims across a worker and the web process.

```python
JobStatus = Literal["active", "waiting_vendor", "waiting_user", "blocked",
                    "done", "failed", "cancelled", "expired"]

class Job(_Model):
    job_id: str
    plan_id: str
    kind: Literal["collect_quotes", "confirm_booking", "chase_missing_info"]
    goal: str                        # "3 comparable caterer quotes, 120 vegetarian guests, cap INR 80,000"
    category: VendorCategory
    target_vendor_ids: tuple[str, ...]
    success: SuccessCriteria         # e.g. min_quotes=3, all_within_cap=True, needs_confirmation=False
    deadline: datetime
    status: JobStatus
    next_action: str                 # human-readable: "nudge Royal Rasoi if silent by 2026-11-02 10:00"
    wake_at: datetime | None
    grant_id: str | None             # standing permission, section 7
    version: int                     # optimistic lock for claims
    history: tuple[JobEvent, ...]    # append-only: ts, actor (worker|user|vendor), event, evidence refs

class SuccessCriteria(_Model):
    min_quotes: int
    max_amount_inr: int | None
    require_written_confirmation: bool = False
```

Add a `job_id` field to `OutreachDraft` and `InboundMessage` (optional, default None) so threads link to jobs without migrating old data. Keep `source` provenance fields as they are.

### Transitions (only these are legal; each logs a `JobEvent`)

| From | Event | To |
|---|---|---|
| active | drafts created, awaiting your approval | waiting_user |
| waiting_user | you approve / grant covers | active |
| active | request sent | waiting_vendor (wake_at = sent + reply window) |
| waiting_vendor | new reply, claims extracted | active (or waiting_user to confirm claims) |
| waiting_vendor | wake_at passed, no reply, nudge allowed by grant | active (nudge drafted, auto-sent only if granted) |
| waiting_vendor | no reply, nudges used up | active (mark vendor `unresponsive`, try next candidate or ask you) |
| any | action outside grant, vendor question, price above cap | waiting_user |
| active | success criteria met with evidence | done |
| any | deadline passed | expired (with a summary of what is missing) |
| any | send `outcome_unknown`, `failed`, `expired` or `denied` on a request | blocked until you check the vendor chat and resolve the item |

<div style="page-break-before:always"></div>

## 5. The worker

`eventmate/worker.py`. One function does the work and is safe to call any number of times.

```python
def tick(now) -> TickReport:
    for job in store.due_jobs(now, limit=MAX_JOBS_PER_TICK):      # wake_at <= now, status in active/waiting_*
        with store.claim(job.job_id, job.version):                 # compare-and-swap; skip if another worker has it
            observe(job)      # conv.poll() for its threads; extract claims from new replies
            decide(job)       # rule table below, returns Action list, max MAX_ACTIONS_PER_RUN
            act(job)          # propose/send via ConversationService; update ledger only after confirmation
            store.finish(job, next_status, next_action, wake_at)   # ALWAYS sets a next state and a wake time
```

How it runs: a loop in a separate process (`python -m eventmate.worker --every 60`) or a systemd timer / cron calling `tick` once. Inbound speed-up: WAHA webhook and an IMAP IDLE or 1-2 minute poll insert an "event" row that sets `wake_at = now` for the matching job. Polling is the fallback, not the main path.

### Decision rules (no model needed for most of it)

1. Drafts missing for a target vendor with a saved contact: create via `request_all`-style call, status -> waiting_user (or auto-approve if the grant allows first contact; by default it does not).
2. Reply with a price mention: create a **claim set** (section 6), notify you, wait for one-tap confirm.
3. Reply that says unavailable: mark vendor out, pick next candidate from the plan's shortlist, draft.
4. Vendor asked a question: create a waiting_user item containing the question and a draft answer using only plan facts (guest count, date, diet), never price or commitment.
5. Silence past the reply window: follow-up draft from fixed template (`clean_followup`), sent only under a grant, max N per thread (already enforced by `SendPolicy`).
6. Enough confirmed quotes within cap: run `observations.compare` plus `vendorscore`, produce a recommendation, ask you to choose. Never book.

Use an LLM only for: classifying ambiguous replies into the claim schema, and drafting reply text from a template plus plan facts. Both outputs go through the same guards you already have (`llm.py` rejects changed numbers, markup, links).

## 6. Closing the reply loop (the main missing link)

Today `ReplyAnalysis` quotes amounts but nothing records them. Add `eventmate/claims.py`:

```python
class QuoteClaim(_Model):
    claim_id: str
    job_id: str
    vendor_id: str
    message_id: str            # InboundMessage.message_id: provenance, always required
    snippet: str               # the vendor's own words, <= 200 chars
    amount_inr: int; basis: Literal["total", "per_plate"]
    guest_count: int | None
    valid_until: date | None
    inclusions: tuple[str, ...]; exclusions: tuple[str, ...]
    status: Literal["proposed", "confirmed", "rejected", "superseded"]
    extractor: Literal["rule:reply_analysis", "llm:<model>"]
    confidence: float
```

Flow: `analyze_reply` (rules) first, an LLM extraction only for what rules miss, then **check the claim against the message text** (the amount string must literally appear in the body, same idea as your LLM number guard). You confirm in one tap (WhatsApp reply "1 yes" or a UI card). On confirm, call `ObservationStore.add_quote(... source_type=VENDOR_QUOTE, evidence_ref=message_id, entered_by="user_confirmed")`, which keeps the "nothing becomes a quote until you record it" rule in ADR-025. Then trigger `Replanner` and `TradeoffNegotiator` and attach their proposal to the job as a user decision, not an automatic plan change.

Attachments are counted but never opened today. A PDF quote is common, so plan a later step: extract text from PDF attachments, treat as untrusted, same claim path.

## 7. Permissions: exact-text approval plus the one standing permission

Keep the existing hash approval as the default for first contact. Add an optional, revocable Grant for narrow, repetitive actions.

```python
class Grant(_Model):
    grant_id: str; job_id: str
    allow: tuple[Literal["followup_template", "answer_from_plan_facts", "request_to_saved_contact"], ...]
    vendors: tuple[str, ...]
    max_messages_per_vendor: int            # e.g. 2
    max_total_messages: int
    forbid: tuple[str, ...] = ("price_offer", "booking", "payment", "new_recipient", "attachment")
    expires_at: datetime
    created_by: str                         # you, with a timestamp and the exact text you agreed to
    revoked_at: datetime | None
```

Rules: the worker may auto-send only if (a) a live grant covers this exact action type and vendor, (b) the body comes from a fixed template or plan facts, (c) counters remain, (d) the existing `SendPolicy` limits pass. Everything else becomes a waiting_user item. Money, booking, new recipients, price positions, and anything a vendor asked that is not in plan facts always go to you. Log the grant id on every auto-sent draft. A single "revoke all" command sets `revoked_at` on all grants and stops the worker's sends (this is the roadmap's F12/F13 wallet and approval queue in smaller form).

## 8. Reliability, recovery, and bounded execution

- **Idempotency**: your claim-then-send plus `outcome_unknown` is the right base. Add an `action_id` (job_id + vendor_id + step + template hash) with a unique constraint so a re-run of `decide` cannot create a second draft for the same step.
- **Unknown outcomes**: keep the rule "never auto-retry". Surface in the digest: "Did the message to X go out? Check the chat, then tap Sent or Not sent." Add a one-tap resolve that moves the draft to `marked_sent` or `failed`.
- **Restart**: all state lives in SQLite and the JSONL logs; `tick` rebuilds from `wake_at`. Test by killing the worker between claim and finish.
- **Bounds per tick**: max jobs, max actions per job, max LLM calls and tokens per job per day (reuse the assistant's allowance code), hard wall-clock per tick, and a global kill switch file or env flag checked every loop.
- **Stuck jobs**: a job with no state change for 72 h and no pending wake escalates to you.
- **Poison input**: vendor text is data. Keep your existing rule and add tests for replies that try to instruct the agent (change recipient, reveal budget, ignore limits). The budget must never be in any outbound template (already a rule in ADR-042).
- **Storage**: the move to SQLite also fixes the multi-process write problem. Put outreach and ledger behind the same interface later (roadmap F23).

## 9. Testing and evals (before you trust it)

Build a **FakeVendor** channel implementing your `Channel` interface that can script behaviors:

| Scenario | Expected result |
|---|---|
| Replies in 2 min with a clear price | claim proposed, you confirm, ledger updated, job counts 1 of 3 |
| Replies after the reply window | late reply still matches thread and is processed |
| Never replies | 2 granted nudges, then vendor marked unresponsive, next candidate drafted |
| Replies twice with different prices | older claim superseded, you are asked which is current |
| Asks "is it for children too?" | waiting_user with the question and a plan-facts draft |
| Price above cap | goes to you with the trade-off options, no auto-reply |
| Message tries to inject instructions | ignored as data, noted, no action |
| Send times out | `outcome_unknown`, no resend, digest asks you |
| Worker killed mid-tick | no duplicate send after restart |
| Grant expired or revoked | no auto-send, becomes a waiting_user item |

Add a small labelled set (50 to 100 real or realistic vendor replies in English and Hinglish) to score the claim extractor: precision of amounts and basis, recall of questions, and false-confirmation rate (target: zero confident wrong prices reaching the ledger without your tap). Measure per job: messages sent per confirmed quote, hours to 3 quotes, number of times you were asked, tokens per job. These are the numbers to compare against any other system, including yours later.

## 10. Cost and latency controls

Rules-first keeps most ticks free. Cache claim extraction by message hash. Use a small model for classification and drafting, a larger one only on low-confidence extraction. Cap tokens per job per day. Batch several vendor replies of one job into one extraction call. Sleep between ticks via wake times, not tight polling. Log tokens and latency per tick to the existing JSONL logs so you can see what is expensive.

## 11. User updates

`eventmate/notify.py`: a digest sent through the channel you already use (a WhatsApp number via your own WAHA, or email), only when something needs you or a job finishes. Format: "Caterer job: 2 of 3 quotes in. Royal Rasoi INR 66,000 total (confirm 1 / not 2). Annapurna asks: is it for children too? Draft answer: ... (send 3 / edit 4)". Reply tokens map to decisions bound to the item's content hash, same as approvals. Do not send price positions or your budget in these notices to anyone but you.

<div style="page-break-before:always"></div>

## 12. Phased rollout

| Phase | Build | Done when |
|---|---|---|
| 0 (1 to 2 days) | Reproducible install (fix `requirements.txt` line, document solver setup, a container or script), CI that runs tests with the solvers, one full plan test | Fresh machine reaches green tests, count matches the doc |
| 1 (3 to 5 days) | `jobs.py` (SQLite, transitions, history) and the web card; FakeVendor channel | Job moves through states by hand and via FakeVendor tests |
| 2 (3 to 5 days) | `worker.tick`, timers, webhook/poll events, wake times, kill switch | Kill and restart test passes; no duplicate sends |
| 3 (4 to 6 days) | Claims: rule extraction, optional LLM extraction, confirm flow, ledger and replan hook | Labelled set meets the thresholds you set |
| 4 (3 to 4 days) | Grants (nudges and templates), revoke-all, notifier digest | Silent vendor handled with zero clicks except the first approval |
| 5 | Live pilot: one real mailbox or WAHA test number, 1 category, 3 vendors, supervised | Real job completes with evidence; log every surprise |
| Later | Attachments (PDF quotes), tradition and accessibility packs, multi-user auth and tenants (F23), payments handoff (user pays, you mark paid) | Per roadmap |

Estimates are rough and assume one developer working on this part only.

## 13. Risks and open decisions

- WAHA is not the official WhatsApp API. A number can be restricted. Use a number you can afford to lose, or use the official Business API with templates before any real pilot.
- Legal checks in ADR-018 remain yours before any send leaves your account.
- Auto-nudging vendors reflects on you. Start with two nudges at 24 to 48 h and no weekend sends, and review the transcripts weekly.
- Decision for you: your GitHub repo is unchanged; only a separate working copy was modified, and the changes are in `followthrough.patch`. Next optional step: you apply the patch yourself (`git apply followthrough.patch`) on a branch, or you tell us to push a branch or open a PR. Neither has been done or authorised.

## Appendix: correction to the earlier status note

Earlier I wrote that the repo has no idempotency layer for sends. That was wrong: the claim-before-send, lease and `outcome_unknown` handling exist in `conversations.py` (verified in source, not run end to end against a real channel). The remaining gaps are the missing action ids, user resolution of unknown outcomes, and multi-process storage.


## Integration decisions (ADR-044)

1. The original design embedded a per-job `Grant`. In this repository a standing permission already exists (ADR-043), so the job's nudge limit lives on the job (`max_nudges_per_vendor`) and the authority to send automatically stays with the permission: revoke, expiry, the kill switch, the 09:00-20:00 IST window, the caps and the disclosure line apply to every automatic message, checked when it is queued and again at dispatch.
2. A stopped job and a revoked permission each prevent sending on their own: ending a job denies its queued automatic messages, and dispatch vetoes any automatic follow-up whose job has ended even if that cleanup did not run.
3. Confirming a claim never invents a validity. The vendor's own statement ("valid for 10 days") is shown as a suggestion; otherwise the user chooses 1-90 days, and the quote's note records that it was the user's choice.
4. The job stores the recipient it started with. Editing a saved contact later does not redirect a running job.
5. Two systems may observe a reply or a silence, but only one claims the outbound action: each automatic follow-up carries an `action_key` (`reply:<message>` or `nudge:<job>:<vendor>:<n>`), unique among live drafts.
6. `OutreachStore` read-check-write steps take an exclusive file lock in addition to the in-process lock. Without it, 12 concurrent processes all claimed the same send in a test.
