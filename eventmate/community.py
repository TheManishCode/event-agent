"""Community events (ADR-046): profiles as data, the checks that can be computed, and the conditional checklist that only the user can mark.

Everything that differs between kinds of community event (keywords that identify it, budget shares, the usual event window, suggested volunteer
roles, which conditional items always apply) is read from `data/community_profiles.json`, never hard-coded. Pick the demo subtype, or add one, by
editing that file; set EVENTMATE_COMMUNITY_PROFILES to use another file.

Three classes of requirement stay apart:
  A. hard checks computed from the user's input and the database (``evaluate`` below, called by the Validator);
  B. conditional requirements: shown when the user's answers trigger them; each ends as confirmed_by_user, not_applicable_by_user or not_verified,
     and EventMate never marks one itself and never states a legal rule;
  C. suggestions (ideas.py).
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from eventmate.agents.vendor import DATA_DIR
from eventmate.jsonl import JsonlLog
from eventmate.netutil import loads
from eventmate.schemas import (
    USER_ITEM_STATUSES, AccessRecord, ChecklistRecord, CommunityAnswers, CommunityDetails, CommunityStatus, ConditionalItem, ConstraintCheck, PlanDraft, PlanVersion,
    UnverifiedItem, VendorCategory,
)

PROFILES_PATH = DATA_DIR / "community_profiles.json"
_SLUG = re.compile(r"^[a-z0-9_]{1,40}$")
_FORBIDDEN_WORDS = re.compile(r"\b(?:safe|compliant|compliance|legal|legally|mandatory)\b", re.I)  # the data may never claim safety, compliance or a legal rule
_ANSWER_KEYS = tuple(CommunityAnswers.model_fields)


class ProfileError(Exception):
    """The community profile file is malformed. Loading fails closed instead of guessing."""


@dataclass(frozen=True)
class Profile:
    key: str
    label: str
    keywords: Tuple[str, ...]
    weights: Dict[str, int]
    start_hour: int
    minutes: int
    roles: Tuple[Tuple[str, str], ...]
    always_items: Tuple[str, ...]


@dataclass(frozen=True)
class ItemDef:
    item_id: str
    title: str
    check_with: str
    triggers: Tuple[str, ...]
    when_service: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Profiles:
    version: int
    reviewed: bool
    default: str
    profiles: Dict[str, Profile]
    items: Tuple[ItemDef, ...]

    def get(self, subtype: str) -> Profile:
        if subtype not in self.profiles:
            raise ValueError(f"unknown community subtype {subtype!r}; known: {', '.join(sorted(self.profiles))}")
        return self.profiles[subtype]


def _fail(msg: str):
    raise ProfileError("community profiles: " + msg)


def _clean(text, what: str, limit: int = 200) -> str:
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= limit or any(ord(c) < 32 or ord(c) == 127 for c in text):
        _fail(f"{what} must be 1-{limit} characters of plain text")
    if _FORBIDDEN_WORDS.search(text):
        _fail(f"{what} may not claim safety, compliance or a legal rule: {text[:60]!r}")
    return text.strip()


def parse_profiles(doc) -> Profiles:
    if not isinstance(doc, dict) or doc.get("schema_version") != 1 or not isinstance(doc.get("version"), int) or not isinstance(doc.get("reviewed"), bool):
        _fail("unsupported schema_version or missing version/reviewed")
    raw = doc.get("profiles")
    if not isinstance(raw, dict) or not raw:
        _fail("no profiles")
    defs: List[ItemDef] = []
    for it in doc.get("conditional_items", []):
        if not isinstance(it, dict) or set(it) - {"id", "title", "check_with", "triggers", "when_service"}:
            _fail("a conditional item has unknown or missing fields")
        iid = it.get("id")
        if not isinstance(iid, str) or not _SLUG.fullmatch(iid) or any(d.item_id == iid for d in defs):
            _fail(f"bad or duplicate conditional item id {iid!r}")
        trig = tuple(it.get("triggers") or ())
        if not trig or any(t not in _ANSWER_KEYS for t in trig):
            _fail(f"{iid}: triggers must be answer names ({', '.join(_ANSWER_KEYS)})")
        svc = tuple(it.get("when_service") or ())
        if any(s not in {c.value for c in VendorCategory} for s in svc):
            _fail(f"{iid}: when_service must be vendor categories")
        defs.append(ItemDef(iid, _clean(it.get("title"), f"{iid}.title"), _clean(it.get("check_with"), f"{iid}.check_with"), trig, svc))
    item_ids = {d.item_id for d in defs}
    out: Dict[str, Profile] = {}
    seen_kw: Dict[str, str] = {}
    for key, p in raw.items():
        if not _SLUG.fullmatch(key or "") or not isinstance(p, dict) or set(p) - {"label", "keywords", "budget_weights_bp", "event_window", "suggested_roles", "always_items"}:
            _fail(f"profile {key!r} has a bad name or unknown fields")
        kws = tuple(k.strip().lower() for k in p.get("keywords", []) if isinstance(k, str) and k.strip())
        if not kws or len(kws) != len(p.get("keywords", [])):
            _fail(f"{key}: keywords must be non-empty text")
        for k in kws:
            if k in seen_kw and seen_kw[k] != key:
                _fail(f"keyword {k!r} appears in both {seen_kw[k]} and {key}")
            seen_kw[k] = key
        w = p.get("budget_weights_bp")
        if not isinstance(w, dict) or set(w) != {c.value for c in VendorCategory} or any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in w.values()) or sum(w.values()) <= 0:
            _fail(f"{key}: budget_weights_bp must give a non-negative whole number for venue, caterer and photographer")
        win = p.get("event_window") or {}
        if not (isinstance(win.get("start_hour"), int) and 0 <= win["start_hour"] <= 23 and isinstance(win.get("minutes"), int) and 30 <= win["minutes"] <= 1440):
            _fail(f"{key}: event_window needs start_hour 0-23 and minutes 30-1440")
        roles = tuple((_clean(r.get("role"), f"{key} role", 40), _clean(r.get("note", "-"), f"{key} role note")) for r in p.get("suggested_roles", []) if isinstance(r, dict))
        always = tuple(p.get("always_items") or ())
        if any(a not in item_ids for a in always):
            _fail(f"{key}: always_items must be conditional item ids")
        out[key] = Profile(key, _clean(p.get("label"), f"{key}.label", 60), kws, dict(w), win["start_hour"], win["minutes"], roles, always)
    default = doc.get("default_subtype")
    if default not in out:
        _fail("default_subtype is not a profile")
    return Profiles(doc["version"], doc["reviewed"], default, out, tuple(defs))


_CACHE: Dict[Tuple[str, int], Profiles] = {}


def load_profiles(path: Optional[Path] = None) -> Profiles:
    """The validated profiles. Re-read when the file changes, so editing the data changes behaviour with no code edit."""
    path = Path(path or os.environ.get("EVENTMATE_COMMUNITY_PROFILES") or PROFILES_PATH)
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError as exc:
        raise ProfileError(f"community profiles unreadable: {exc}") from None
    if key not in _CACHE:
        try:
            _CACHE.clear()
            _CACHE[key] = parse_profiles(loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise ProfileError(f"community profiles unreadable: {exc}") from None
    return _CACHE[key]


def profile_for(request) -> Profile:
    """The profile that applies to a community request. An unknown subtype falls back to the default here; the Validator then fails the plan
    on `community_profile` with the known subtypes listed, so a typo is never silently accepted."""
    profiles = load_profiles()
    sub = request.community.subtype if getattr(request, "community", None) is not None else profiles.default
    return profiles.profiles.get(sub) or profiles.profiles[profiles.default]


def match_subtype(text: str, profiles: Optional[Profiles] = None) -> Optional[Tuple[str, str]]:
    """(subtype, keyword) for the longest profile keyword found in the text, or None. Keywords are whole words or phrases."""
    profiles = profiles or load_profiles()
    best: Optional[Tuple[int, int, str, str]] = None
    for order, (key, p) in enumerate(profiles.profiles.items()):
        for kw in p.keywords:
            if re.search(r"(?<![\w-])" + r"\s+".join(map(re.escape, kw.split())) + r"(?![\w-])", text, re.I):
                cand = (len(kw), -order, key, kw)
                if best is None or cand[:2] > best[:2]:
                    best = cand
    return (best[2], best[3]) if best else None


# --------------------------------------------------- venue accessibility records


class _AccessFile(JsonlLog[AccessRecord]):
    channel = "access"
    model = AccessRecord


class AccessibilityStore:
    """What venues told the user, append-only. The latest record for a venue wins. Venue records themselves say nothing unless the seed data does."""

    def __init__(self, root: Path, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.root = Path(root) / "accessibility"
        self.now = now
        self._lock = threading.RLock()

    def _file(self) -> _AccessFile:
        return _AccessFile(self.root, run_id="all")

    def record(self, venue_id: str, features: Sequence[str], evidence_ref: str, entered_by: str) -> AccessRecord:
        rec = AccessRecord(venue_id=venue_id, features=tuple(features), evidence_ref=evidence_ref, entered_by=entered_by, recorded_at=self.now())
        with self._lock:
            return self._file()._append(rec, venue_id)

    def latest(self, venue_id: str) -> Optional[AccessRecord]:
        found = [r for r in self._file().read() if r.venue_id == venue_id]
        return found[-1] if found else None

    def all(self) -> List[AccessRecord]:
        latest: Dict[str, AccessRecord] = {}
        for r in self._file().read():
            latest[r.venue_id] = r
        return list(latest.values())


# ------------------------------------------------------------ class A: hard checks


def _chk(name: str, passed: bool, detail: str) -> ConstraintCheck:
    return ConstraintCheck(name=name, passed=passed, detail=detail)


def _hhmm(minutes: int, origin: datetime) -> str:
    from datetime import timedelta

    return (origin + timedelta(minutes=minutes)).strftime("%H:%M")


def _phase_windows(d: PlanDraft) -> Dict[str, Tuple[int, int]]:
    e = {x.task_id: x for x in d.solution.entries}
    out: Dict[str, Tuple[int, int]] = {}
    for phase, task in (("setup", "venue_setup"), ("event", "event"), ("teardown", "teardown")):
        if task in e:
            out[phase] = (e[task].start, e[task].end)
    return out


def _attendance(d: PlanDraft, repo) -> ConstraintCheck:
    r = d.request
    venue = next((s for s in d.selections if s.category == VendorCategory.VENUE), None)
    rec = repo.get(VendorCategory.VENUE, venue.vendor_id) if venue else None
    cap = getattr(rec, "capacity", 0)
    att, c = r.planning_attendance, r.community
    rng = f" (range {c.attendance_low or r.guest_count}-{c.attendance_high or r.guest_count})" if c and (c.attendance_low or c.attendance_high) else ""
    return _chk("attendance_vs_capacity", cap >= att, f"expected {r.guest_count} guests{rng}, planning for {att} against venue capacity {cap}; headroom {cap - att}")


def _funding(d: PlanDraft) -> ConstraintCheck:
    det, r = d.request.community or CommunityDetails(), d.request
    if det.own_funds_inr is None and not det.funding:
        return _chk("funding_vs_budget", True, f"no funding breakdown given: the budget of INR {r.budget_total:,} is treated as your own funds")
    sums = {s: sum(f.amount_inr for f in det.funding if f.status == s) for s in ("received", "committed", "pledged")}
    own = det.own_funds_inr or 0
    basis = own + sums["received"] + (sums["committed"] if det.count_committed else 0)
    total = sum(s.cost for s in d.selections)
    need = total + d.allocation.contingency
    counted = f"committed {sums['committed']:,} counted (you chose to count it)" if det.count_committed else f"committed {sums['committed']:,} not counted"
    detail = (
        f"the plan may spend INR {basis:,}: own {own:,} + received {sums['received']:,}" + (f" + committed {sums['committed']:,}" if det.count_committed else "")
        + f"; {counted}; pledged {sums['pledged']:,} never counted. Vendors INR {total:,} + reserve INR {d.allocation.contingency:,} = INR {need:,}"
    )
    if basis < need:
        detail += f". Short by INR {need - basis:,}"
    return _chk("funding_vs_budget", basis >= need, detail)


def _volunteers(d: PlanDraft) -> Tuple[Optional[ConstraintCheck], List[UnverifiedItem]]:
    det = d.request.community or CommunityDetails()
    in_play = bool(det.volunteers or det.slots or det.shifts)
    unverified: List[UnverifiedItem] = []
    if not in_play and not (det.setup_owner or det.cleanup_owner):
        unverified.append(UnverifiedItem(
            name="setup_cleanup_owners", detail="nobody is named to own setup and cleanup",
            repair="name a setup owner and a cleanup owner and add them to the volunteer list",
        ))
    windows = _phase_windows(d)
    problems: List[str] = []
    abs_shifts: Dict[str, List[Tuple[int, int, str]]] = {}
    for sh in det.shifts:
        if sh.phase not in windows:
            continue
        ps, pe = windows[sh.phase]
        a, b = ps + sh.start_min, min(pe, ps + (sh.end_min if sh.end_min is not None else pe - ps))
        if b > a:
            abs_shifts.setdefault(sh.volunteer, []).append((a, b, f"{sh.role} ({sh.phase})"))
    for slot in det.slots:
        if slot.phase not in windows:
            problems.append(f"{slot.role} ({slot.phase}): the plan has no {slot.phase} phase")
            continue
        ps, pe = windows[slot.phase]
        s0, s1 = ps + slot.start_min, ps + (slot.end_min if slot.end_min is not None else pe - ps)
        if s0 >= pe or s1 > pe or s1 <= s0:
            problems.append(f"{slot.role} ({slot.phase}): the slot lies outside its phase ({pe - ps} minutes long)")
            continue
        role = slot.role.casefold()
        covering = [(sh, ps + sh.start_min, min(pe, ps + (sh.end_min if sh.end_min is not None else pe - ps))) for sh in det.shifts if sh.phase == slot.phase and sh.role.casefold() == role]
        points = sorted({s0, s1} | {p for _, a, b in covering for p in (a, b) if s0 < p < s1})
        worst, where = slot.needed, (s0, s1)
        for a, b in zip(points, points[1:]):
            n = len({sh.volunteer for sh, x, y in covering if x <= a and y >= b})
            if n < worst:
                worst, where = n, (a, b)
        if worst < slot.needed:
            problems.append(f"{slot.role} ({slot.phase}) has {worst} of {slot.needed} from {_hhmm(where[0], d.origin)} to {_hhmm(where[1], d.origin)}")
    doubled = sorted(
        who for who, spans in abs_shifts.items()
        if any(spans[i][0] < spans[j][1] and spans[j][0] < spans[i][1] for i in range(len(spans)) for j in range(i + 1, len(spans)))
    )
    if doubled:
        problems.append("double-booked at the same time: " + ", ".join(doubled))
    if in_play:
        for label, who in (("setup", det.setup_owner), ("cleanup", det.cleanup_owner)):
            if who is None:
                problems.append(f"{label} has no owner")
    if not in_play:
        return _chk("volunteer_coverage", True, "no volunteer roles confirmed yet (suggested roles are prompts to confirm, not requirements)"), unverified
    if not det.slots and not problems:
        return _chk("volunteer_coverage", True, "no volunteer roles confirmed yet (suggested roles are prompts to confirm, not requirements); owners named"), unverified
    return _chk("volunteer_coverage", not problems, "every confirmed role is covered, nobody is double-booked, setup and cleanup have owners" if not problems else "; ".join(problems)), unverified


def _timeline(d: PlanDraft, policy) -> ConstraintCheck:
    e = {x.task_id: x for x in d.solution.entries}
    problems: List[str] = []
    if all(k in e for k in ("venue_setup", "event", "teardown")):
        if e["venue_setup"].end + policy.setup_buffer_minutes > e["event"].start:
            problems.append("setup does not finish before doors open")
        if e["teardown"].start < e["event"].end:
            problems.append("cleanup starts before the event closes")
    else:
        problems.append("setup, event or cleanup is missing from the schedule")
    det = d.request.community or CommunityDetails()
    by_resource: Dict[str, List[Tuple[int, int, str]]] = {}
    for a in det.program:
        entry = e.get(f"activity_{a.activity_id}")
        if entry is None:
            problems.append(f"{a.title} is not scheduled")
            continue
        if entry.end - entry.start != a.minutes:
            problems.append(f"{a.title} has the wrong length")
        if "event" in e and (entry.start < e["event"].start or entry.end > e["event"].end):
            problems.append(f"{a.title} falls outside the event")
        if a.after and f"activity_{a.after}" in e and entry.start < e[f"activity_{a.after}"].end:
            problems.append(f"{a.title} starts before {a.after} has finished")
        by_resource.setdefault(a.resource, []).append((entry.start, entry.end, a.title))
    for res, spans in by_resource.items():
        spans.sort()
        for (s1, e1, t1), (s2, e2, t2) in zip(spans, spans[1:]):
            if s2 < e1:
                problems.append(f"{t1} and {t2} overlap on {res}")
    return _chk("timeline_dependencies", not problems, "setup ends before doors, cleanup follows the close" + (f", {len(det.program)} programme activities fit with no clash" if det.program else "") if not problems else "; ".join(problems))


def _accessibility(d: PlanDraft, repo) -> Tuple[Optional[ConstraintCheck], List[UnverifiedItem]]:
    det = d.request.community
    if det is None or not det.access_needs:
        return None, []
    venue = next((s for s in d.selections if s.category == VendorCategory.VENUE), None)
    declared = repo.venue_access(venue.vendor_id) if venue else None
    if declared is None:
        return None, [UnverifiedItem(
            name="accessibility_declared", detail=f"the venue has declared nothing about: {', '.join(det.access_needs)}",
            repair="ask the venue and record the answer (with where it came from) so this can be checked",
        )]
    have = [n for n in det.access_needs if n in declared]
    lack = [n for n in det.access_needs if n not in declared]
    if lack:
        return _chk("accessibility_declared", False, f"the venue declares {', '.join(declared) or 'none of these'} but not: {', '.join(lack)}"), []
    return _chk("accessibility_declared", True, f"the venue declares: {', '.join(have)}"), []


def evaluate(d: PlanDraft, repo, policy) -> Tuple[List[ConstraintCheck], List[UnverifiedItem]]:
    """The community hard checks for a draft, re-derived from the request, the schedule and the database. (checks, unverified)."""
    if d.request.event_type.value != "community":
        return [], []
    det = d.request.community or CommunityDetails()
    known = load_profiles().profiles
    checks = [_chk("community_profile", det.subtype in known, f"subtype {det.subtype}" + ("" if det.subtype in known else f" is not a known subtype; known: {', '.join(sorted(known))}")), _attendance(d, repo), _funding(d)]
    vol, unverified = _volunteers(d)
    if vol is not None:
        checks.append(vol)
    checks.append(_timeline(d, policy))
    acc, unknown = _accessibility(d, repo)
    if acc is not None:
        checks.append(acc)
    return checks, unverified + unknown


def summary(details: Optional[CommunityDetails]) -> str:
    """One line for the plan diff and the UI."""
    if details is None:
        return "none"
    parts = [details.subtype.replace("_", " ")]
    if details.attendance_low or details.attendance_high:
        parts.append(f"attendance {details.attendance_low or '?'}-{details.attendance_high or '?'}")
    if details.own_funds_inr is not None or details.funding:
        recv = sum(f.amount_inr for f in details.funding if f.status == "received")
        parts.append(f"funding: own {details.own_funds_inr or 0:,}, received {recv:,}, {len(details.funding)} source(s)")
    if details.volunteers or details.slots:
        parts.append(f"{len(details.volunteers)} volunteers, {len(details.slots)} confirmed roles")
    if details.program:
        parts.append(f"{len(details.program)} programme activities")
    if details.access_needs:
        parts.append("access: " + ", ".join(details.access_needs))
    return "; ".join(parts)


# ------------------------------------------- class B: conditional requirements


class _ChecklistFile(JsonlLog[ChecklistRecord]):
    channel = "checklist"
    model = ChecklistRecord


class ChecklistStore:
    """The user's marks on conditional items. EventMate writes here only when the user acts; nothing in this module marks an item itself."""

    def __init__(self, root: Path, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.root = Path(root) / "community"
        self.now = now
        self._lock = threading.RLock()

    def _file(self, plan_id: str) -> _ChecklistFile:
        return _ChecklistFile(self.root, run_id=plan_id)

    def latest(self, plan_id: str) -> Dict[str, ChecklistRecord]:
        out: Dict[str, ChecklistRecord] = {}
        for r in self._file(plan_id).read():
            out[r.item_id] = r
        return out

    def mark(self, plan_id: str, item_id: str, shown: Sequence[str], status: str, by, on, note: str, today) -> ChecklistRecord:
        if item_id not in shown:
            raise ValueError(f"{item_id!r} is not a requirement shown for this plan")
        if status not in USER_ITEM_STATUSES:
            raise ValueError("a status can only be one of: " + ", ".join(USER_ITEM_STATUSES))
        if not isinstance(by, str) or not by.strip():
            raise ValueError("say who confirmed it (a name)")
        if on > today:
            raise ValueError("the date cannot be in the future")
        if status == "not_applicable_by_user" and not (note or "").strip():
            raise ValueError("give a reason it does not apply")
        rec = ChecklistRecord(plan_id=plan_id, item_id=item_id, status=status, by=by, on=on, note=note or "", recorded_at=self.now())
        with self._lock:
            return self._file(plan_id)._append(rec, item_id)


def shown_items(request, profiles: Optional[Profiles] = None) -> List[Tuple[ItemDef, Tuple[str, ...]]]:
    """Conditional items for a request with what triggered each. Only an explicit "no" hides an item; "unsure" and unanswered show it."""
    profiles = profiles or load_profiles()
    det = request.community or CommunityDetails()
    prof = profile_for(request)
    services = {c.value for c in request.services}
    out = []
    for d in profiles.items:
        why = [f"answer: {t}" for t in d.triggers if getattr(det.answers, t) in ("yes", "unsure")]
        why += [f"service: {s}" for s in d.when_service if s in services]
        if d.item_id in prof.always_items:
            why.append(f"always for {prof.key}")
        if why:
            out.append((d, tuple(why)))
    return out


def conditional_items(request, records: Dict[str, ChecklistRecord]) -> Tuple[ConditionalItem, ...]:
    items = []
    for d, why in shown_items(request):
        rec = records.get(d.item_id)
        items.append(ConditionalItem(
            item_id=d.item_id, title=d.title, check_with=d.check_with, instruction=f"Check with {d.check_with}", triggered_by=why,
            status=rec.status if rec else "not_verified", recorded_by=rec.by if rec else None, recorded_on=rec.on if rec else None, note=rec.note if rec else "",
        ))
    return tuple(items)


def build_status(plan: PlanVersion, store: ChecklistStore, report=None) -> CommunityStatus:
    """``report`` is a fresh validation of the plan against today's data; without it the validation stored with the plan version is used."""
    r = plan.request
    if r.event_type.value != "community":
        raise ValueError("this plan is not a community event")
    profiles = load_profiles()
    prof = profile_for(r)
    items = conditional_items(r, store.latest(plan.plan_id))
    open_n = sum(1 for i in items if i.status == "not_verified")
    summary = "no conditional items apply to your answers" if not items else f"{open_n} conditional item{'' if open_n == 1 else 's'} not verified"
    report = report or plan.validation
    unv = report.unverified
    detail = summary + (f"; {len(unv)} check{'' if len(unv) == 1 else 's'} could not be decided from the data (unverified)" if unv else "") + ". EventMate verifies none of these: each is marked only by you."
    names = {"community_profile", "attendance_vs_capacity", "funding_vs_budget", "volunteer_coverage", "timeline_dependencies", "accessibility_declared"}
    return CommunityStatus(
        plan_id=plan.plan_id, version=plan.version, subtype=prof.key, profile_label=prof.label, profile_version=profiles.version, profile_reviewed=profiles.reviewed,
        items=items, not_verified=open_n, summary=summary, summary_detail=detail, unverified=unv,
        checks=tuple(c for c in report.checks if c.name in names), suggested_roles=prof.roles,
    )
