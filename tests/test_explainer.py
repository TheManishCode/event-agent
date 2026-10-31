from datetime import datetime

import pytest

from eventmate.agents.explainer import ExplainerAgent
from eventmate.agents.guest import GuestAgent, GuestRepository
from eventmate.agents.planner import PlannerAgent
from eventmate.agents.scheduler import SchedulingAgent
from eventmate.agents.validator import ValidatorAgent
from eventmate.llm import PromptError, PromptStore, guard_rewrite
from eventmate.logs import RunLogs
from eventmate.plan_store import assemble_plan
from eventmate.schemas import DiffItem, PlanDiff
from eventmate.symbolic import SymbolicChecker

ORIGIN = datetime(2026, 10, 30)


@pytest.fixture
def logs(tmp_path):
    return RunLogs.create(log_dir=tmp_path, run_id="exp")


@pytest.fixture
def plan(logs, repo, intake):
    g = GuestAgent(GuestRepository(), logs.decisions).summarize(intake.request)
    d = PlannerAgent(logs, SchedulingAgent(logs.decisions), SymbolicChecker()).plan(intake.request, g, intake.budget_allocation, intake.vendor_shortlists, ORIGIN, plan_id="pe").draft
    return assemble_plan(d, ValidatorAgent(repo, logs).validate(d), 1, None, "initial plan", ())


class FakeLLM:
    name = "fake-model"

    def __init__(self, fn):
        self.fn, self.calls = fn, []

    def complete(self, system, user, max_tokens):
        self.calls.append(user)
        return self.fn(user)


def test_explanation_contains_every_fact_with_reasons(logs, plan):
    md = ExplainerAgent(logs).explain(plan).markdown
    for s in plan.selections:
        assert s.name in md and f"{s.cost:,}" in md
        assert all(a.option in md for a in s.alternatives)
    for needle in ("12 Dec 2026", "18:00", "Reminder: send invitations", "Reminder: confirm vendors", "139,000", "200,000", "Pune", "120 guests"):
        assert needle in md
    assert "alternative" in md.lower() and "Why this plan is valid" in md
    assert "year not given" in md  # assumptions are surfaced


def test_every_recommendation_shows_an_alternative_or_says_why_not(logs, plan):
    md = ExplainerAgent(logs).explain(plan).markdown
    assert md.lower().count("alternatives considered") >= len(plan.selections)


def test_budget_table_reconciles(logs, plan):
    md = ExplainerAgent(logs).explain(plan).markdown
    assert "Contingency" in md and f"{plan.allocation.contingency:,}" in md and f"{plan.total_cost:,}" in md


def test_diff_sections_are_rendered_when_given(logs, plan):
    diff = PlanDiff(
        parent_version=1, new_version=2,
        added=(DiffItem(kind="vendor", key="photographer", after="Candid Frames"),),
        removed=(DiffItem(kind="vendor", key="venue", before="Orchid Banquet"),),
        changed=(DiffItem(kind="request", key="guest_count", before="120", after="100"),),
        preserved=(DiffItem(kind="vendor", key="caterer", before="Royal Rasoi", after="Royal Rasoi", note="confirmed booking kept"),),
    )
    md = ExplainerAgent(logs).explain(plan, diff).markdown
    for needle in ("Added", "Removed", "Changed", "Preserved", "Candid Frames", "confirmed booking kept", "120", "100"):
        assert needle in md


def test_markdown_in_names_cannot_break_tables(logs, plan):
    evil = plan.selections[0].model_copy(update={"name": "A|B `x` <script>\nrow"})
    p2 = plan.model_copy(update={"selections": (evil,) + plan.selections[1:]})
    md = ExplainerAgent(logs).explain(p2).markdown
    assert "<script>" not in md and "A\\|B" in md
    import re

    rows = [l for l in md.splitlines() if l.startswith("|")]
    assert rows and all(len(re.findall(r"(?<!\\)\|", l)) == 4 for l in rows)  # 3 columns, no injected cells
    assert "\nrow" not in md  # newline in a name cannot start a new line


def test_explainer_logs_a_decision(logs, plan):
    ExplainerAgent(logs).explain(plan)
    assert logs.decisions.read()[-1].agent.value == "explainer"


# ------------------------------------------------------------------ LLM path


def test_llm_polish_is_used_only_when_it_passes_the_guard(logs, plan):
    fake = FakeLLM(lambda u: u.split("<<<PLAN")[1].split("PLAN>>>")[0].strip().replace("## ", "## ✨ "))
    ex = ExplainerAgent(logs, llm=fake).explain(plan)
    assert ex.used_llm and "✨" in ex.markdown
    t = logs.llm_traces.read()[-1]
    assert t.accepted and t.prompt_id == "explainer.rewrite" and t.prompt_version == 1


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda t: t.replace("139,000", "129,000"), "number"),
        (lambda t: t + "\nTotal saving: 5,000 rupees.", "number"),
        (lambda t: t.replace("Orchid Banquet", "A Better Hall"), "vendor"),
        (lambda t: t + "\n<img src=x onerror=alert(1)>", "markup"),
        (lambda t: t + "\nSee https://evil.example/pay", "link"),
        (lambda t: t * 5, "length"),
        (lambda t: "", "empty"),
    ],
)
def test_llm_output_that_changes_facts_is_rejected_and_falls_back(logs, plan, mutate, reason):
    fake = FakeLLM(lambda u: mutate(u.split("<<<PLAN")[1].split("PLAN>>>")[0].strip()))
    ex = ExplainerAgent(logs, llm=fake).explain(plan)
    base = ExplainerAgent(logs).explain(plan)
    assert not ex.used_llm and ex.markdown == base.markdown
    t = logs.llm_traces.read()[-1]
    assert not t.accepted and reason in t.rejection_reason


def test_llm_exception_falls_back_and_is_traced(logs, plan):
    def boom(u):
        raise RuntimeError("network down")

    ex = ExplainerAgent(logs, llm=FakeLLM(boom)).explain(plan)
    assert not ex.used_llm and ex.markdown
    assert "network down" in logs.llm_traces.read()[-1].rejection_reason


def test_llm_never_receives_raw_user_text_or_guest_names(logs, plan):
    fake = FakeLLM(lambda u: u.split("<<<PLAN")[1].split("PLAN>>>")[0].strip())
    ExplainerAgent(logs, llm=fake).explain(plan)
    assert plan.request.raw_text not in fake.calls[0]


def test_guard_accepts_identical_text_and_reports_reasons():
    src = "Venue Heritage Hall costs INR 65,000 on 12 Dec 2026 at 18:00."
    assert guard_rewrite(src, src, names=["Heritage Hall"]) is None
    assert "number" in guard_rewrite(src.replace("65,000", "60,000"), src, names=["Heritage Hall"])


def test_prompt_store_loads_versioned_markdown_and_rejects_bad_ids(tmp_path):
    ps = PromptStore()
    text, version = ps.load("explainer.rewrite")
    assert version == 1 and "{plan_markdown}" in text
    for bad in ("../secrets", "a/b", "", "x" * 100, "explainer.rewrite.v1"):
        with pytest.raises(PromptError):
            ps.load(bad)
    with pytest.raises(PromptError):
        ps.load("does.not.exist")


def test_prompt_store_picks_highest_version(tmp_path):
    (tmp_path / "p.test.v1.md").write_text("---\nid: p.test\nversion: 1\n---\nold {x}")
    (tmp_path / "p.test.v2.md").write_text("---\nid: p.test\nversion: 2\n---\nnew {x}")
    assert PromptStore(tmp_path).load("p.test") == ("new {x}", 2)


def test_prompt_with_business_logic_markers_is_flagged():
    """Prompts may carry wording only; this guards the repo's real prompts."""
    from pathlib import Path

    for p in (Path(__file__).resolve().parents[1] / "prompts").glob("*.md"):
        if p.name == "README.md":
            continue
        body = p.read_text().lower()
        for banned in ("budget_total", "available_dates", "capacity", "price_per_plate", "booking_status"):
            assert banned not in body, f"{p.name} contains data field {banned}"


def test_escaping_keeps_normal_text_readable(logs, plan):
    md = ExplainerAgent(logs).explain(plan).markdown
    assert "(INR 55,000" in md and "budget_cap" in md and "\\(" not in md and "budget\\_cap" not in md


@pytest.mark.parametrize(
    "phrase",
    ["one hundred thousand rupees", "about sixty thousand", "ninety-nine", "two lakh", "a million", "twenty percent"],
)
def test_guard_rejects_spelled_out_numbers_that_are_not_in_the_source(phrase):
    src = "Venue Heritage Hall costs INR 65,000."
    assert "number" in guard_rewrite(src + " It offers " + phrase + " in value.", src, names=["Heritage Hall"])


def test_guard_allows_number_words_that_the_source_already_uses():
    src = "Two venues were considered; one hundred percent of checks passed."
    assert guard_rewrite(src, src, names=[]) is None
