# Demo script (about 5 minutes)

The community subtype for the demo is a **configuration choice, still undecided**. It is data in `eventmate/data/community_profiles.json` (or the file named by `EVENTMATE_COMMUNITY_PROFILES`). Pick one key from that file, call it `<subtype>`, and use a brief containing one of that profile's keywords. Examples: `college fest` -> `college_fest`; `RWA` -> `neighbourhood_gathering`; `fundraiser` -> `fundraiser`. Nothing in the code depends on which you pick.

**Say this first.** The vendor data is synthetic seed data: mostly Pune, and only venues, caterers and photographers (a handful of Mumbai records). Prices, availability and capacities in the demo are not real. Email is shown with a fake mailbox or a controlled test mailbox, never real vendors.

Setup: `python -m eventmate.web`, open http://127.0.0.1:8765. Mail is the fake world used by the tests (`tests/mailfake.py`) or a dedicated test mailbox you control (see `EMAIL_SETUP.md`). No real vendors.

## 1. Brief to plan (1 min)
In the chat box type: `We want the <keyword for your subtype>, 150 people in Pune on Dec 12, budget 2.5 lakh, venue and catering.`
Show: the review card highlights "Community" in amber ("inferred from your words, please check") and offers a subtype dropdown. Confirm. The plan passes its hard checks.

## 2. Ideas (30 s)
Open **Ideas** -> Suggest ideas. Point at "Suggestions, not verified offers", no prices, the facts each idea used. Pin one (it becomes a checklist item), dismiss one. The plan version does not change.

## 3. A rejected constraint with its repair (1 min)
Open **Community**. Set own funds to 100000 and Save. It is refused: "Funding Vs Budget ... Short by INR 57,500", with "To fix". Nothing was saved. Set 160000 and Save: version 2, with the funding basis stated (own funds only; pledged never counted).
Also show the conditional items: "11 conditional items not verified", each saying "Check with ...". Mark one confirmed with a name; the count drops. EventMate never marks one itself.

### 3b. A required need stays "Needs verification" (1 min)
In **Community**, tick *Step-free access* under "Access needs of your guests" and Save. The plan is built, but it is now **provisional**: the header shows **Needs verification: required accessibility unknown** (not the green "hard constraints passed"), the Overview says what is still open, and the Community tab says what to do. The seed venues declare no accessibility data, so EventMate does not guess.
Now "What Lakeside Lawns told you about access": tick *Step-free access*, type where the answer came from ("email from the venue manager, 3 Nov"), and record it. The header changes to the normal verified badge. Record a later answer that leaves it out and the plan fails the check instead, with its repair.

## 3c. Discovery to a reviewed contact (1 min)
Open **Vendor chats** and the **Find vendors** card. Say which vendors are which: the twelve seed vendors are **synthetic** (mostly Pune); anything found by a search is **discovered** and carries its source; anything labelled **TEST VENDOR (address owned by <you>)** is an address you own and nothing else.
Search for a category. Venues come from OpenStreetMap (free); asking OpenStreetMap for caterers says "Not supported by this source", and caterers and photographers use Google Places, which needs a key. Without a key, use the fake search the tests use (`tests/test_shortlist.py`) and say that it is a fake. Press **Find public contact** on a business: each address shows the page it was found on and any flag ("generic mailbox", "different domain"). Nothing has been contacted. Type your name, correct the address if needed and **Confirm**; only then can the business be ticked for a job. Note the line "Check the anti-spam and privacy rules that apply to you before contacting businesses. EventMate does not verify them."
Then **Draft quote requests**, approve the exact text, and continue as below. If a business replies "please stop messaging us", its address appears under **Do not contact** with the reason; it blocks every plan and job, and only you can lift it, with a reason.

## 4. Vendor request and reply (1 min)
Vendor chats -> save contacts for three caterers, start a job ("2 caterer quotes"), approve the drafted requests (the exact text is shown; no budget in it). A reply arrives (`MailWorld.reply(...)` in the fake world, or reply from your second address): "Total INR 78,000 for the event. Valid for 10 days."
Show the Updates box and the proposed price with "check taxes and extras". It is not a quote yet.

## 5. Confirm and re-price (1 min)
Enter validity (10 days is pre-filled because the vendor wrote it) and confirm: now it is a recorded quote. Show the plan is still unchanged. Budget tab -> "Re-price this plan": version 3 with the lower caterer cost and a diff. No plan change happened automatically.
Optional: turn on the owner email copy; show the exact summary before confirming.

## Paired regression run (30 s)
Run a wedding: `Plan a 120-person wedding in Pune on December 12 under INR 500,000. Venue and catering.` There is no Community tab, no community checks, and the plan, Ideas and quotes flow work as before. `python -m pytest tests` (1696 passed on Python 3.14.8 with solvers and Chromium).
