# Email setup (vendor messages and the optional owner copy)

Email works through a mailbox you own, over SMTP (sending) and IMAP (reading replies). **Status: tested with fake SMTP/IMAP only; not yet validated against a real mailbox** (see `EMAIL_VALIDATION.md`).

## 1. Use a dedicated mailbox
Create a mailbox just for this, not your main one. Vendors will see its address, and the app password sits on this machine.

## 2. Turn on 2-Step Verification and make an app password
Gmail and most providers require 2-Step Verification before they offer app passwords. Create an app password for this app. Never use your normal account password.

## 3. Put the settings in `.env` only
Copy `.env.example` to `.env`, then `chmod 600 .env`. Fill in:

```
EVENTMATE_SMTP_HOST=smtp.gmail.com
EVENTMATE_SMTP_PORT=465
EVENTMATE_SMTP_USER=your.dedicated.mailbox@example.com
EVENTMATE_SMTP_PASSWORD=<the app password>
EVENTMATE_IMAP_HOST=imap.gmail.com
EVENTMATE_IMAP_PORT=993
```

`.env` is git-ignored. Do not put these values in code, tests, docs, logs or commits. Port 465 uses TLS from the start; any other SMTP port uses STARTTLS and never falls back to plain text.

## 4. Check the settings without sending anything
```
python -m eventmate.smoke_email
```
It logs in to SMTP and IMAP (the inbox is opened read-only) and prints only PASS or FAIL and the host. It never prints the user name, the password or the server's own error text. A real one-message test needs both `--send-test-to ADDRESS --i-own-this-address`.

## 5. Sending limits
Providers limit how much you can send. Gmail's limits are described at https://support.google.com/mail/answer/22839. EventMate's own limits are lower: 3 messages per vendor and 30 per day, and the optional owner copy at most 4 a day, 30 minutes apart. Do not raise them without checking your provider's rules.

## 6. How to stop
- **Kill switch:** `python -m eventmate inbox kill` (or the red button in the Vendor chats tab). It stops every automatic message and the owner email copy, and denies messages already queued. `inbox resume` lifts it.
- **Stop file:** set `EVENTMATE_WORKER_STOP=/path/to/STOP` and create that file. The worker loop and the owner email copy stop while it exists.
- **Off for one plan:** turn the automatic follow-up permission off in the Vendor chats tab.
- **Owner email copy:** off by default; turn it off in the Updates box at any time.

## 7. What is never sent
Request templates and automatic follow-ups never contain your budget or a price. Text you type yourself is sent as you wrote it. Vendor replies are untrusted text: they never record a quote, change a plan, or approve anything.
