# Email validation record

**Result: not yet run.** No real mailbox has been used with EventMate. Until this file records a passed run with a date, the README must say: "Email is tested with fake SMTP/IMAP only; not yet validated against a real mailbox."

## Plan (you run this, not the test suite)
Use a dedicated mailbox and recipients you own or who agreed. No real vendors.

1. `python -m eventmate.smoke_email` : expect PASS smtp and PASS imap.
2. Start a job for one vendor whose contact is a second address you own; approve the request; confirm one email arrives at that address.
3. Reply manually from that address with a price such as "Total INR 50,000 for the event."
4. Run `python -m eventmate jobs tick` (or wait for the loop). Confirm: the reply is matched to the right thread, a claim is proposed, no quote is recorded until you confirm, and nothing is sent automatically unless a standing permission is on.
5. Reply from a different address using "Reply" on the sent mail; confirm it is flagged as a different sender and no claim is made.
6. Turn the kill switch on and confirm no automatic message goes out.

## Record (fill in after running)
| Step | Date | Result | Notes |
|---|---|---|---|
| 1 smoke |  |  |  |
| 2 send |  |  |  |
| 3-4 reply and claim |  |  |  |
| 5 different sender |  |  |  |
| 6 kill switch |  |  |  |

## Not tested (must stay listed even after a pass)
Real vendors; provider rate limits and spam filtering; attachments; non-English replies from real people; HTML-heavy mail; long-running operation; multiple mailboxes; WhatsApp (out of scope).

After a pass, the README may say "Validated against a real mailbox on <date>, in a controlled test with approved recipients". It may not claim production or real-vendor readiness.
