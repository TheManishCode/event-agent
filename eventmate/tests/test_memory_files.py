"""Guards the handoff/decision memory files (AI_CODING_RULES.md, "Memory files")."""
import re
from datetime import date
from pathlib import Path

DOCS = Path(__file__).resolve().parents[2] / "docs"
RULES = DOCS.parent / "AI_CODING_RULES.md"


def _read(name: str) -> str:
    path = DOCS / name
    assert path.is_file(), f"{name} is missing; the memory-file rule requires it"
    return path.read_text(encoding="utf-8")


def test_decisions_are_numbered_consecutively_with_status():
    text = _read("DECISIONS.md")
    numbers = [int(n) for n in re.findall(r"^## ADR-(\d{3}): \S", text, re.M)]
    assert numbers, "no ADR entries found"
    assert numbers == list(range(1, len(numbers) + 1))
    assert len(re.findall(r"^Status: ", text, re.M)) == len(numbers)


def test_handoff_has_required_sections_and_valid_date():
    text = _read("HANDOFF.md")
    for heading in ("Current state", "Next step", "Open questions", "Session log"):
        assert re.search(rf"^## .*{heading}", text, re.M), f"HANDOFF.md lacks '{heading}'"
    match = re.search(r"^Last updated: (\d{4}-\d{2}-\d{2})$", text, re.M)
    assert match, "HANDOFF.md needs a 'Last updated: YYYY-MM-DD' line"
    assert date.fromisoformat(match.group(1)) <= date.today()


def test_handoff_session_log_is_not_older_than_last_updated():
    text = _read("HANDOFF.md")
    updated = re.search(r"^Last updated: (\S+)$", text, re.M).group(1)
    log = text.split("## Session log", 1)[1]
    newest = re.search(r"^- (\d{4}-\d{2}-\d{2}):", log, re.M)
    assert newest and newest.group(1) == updated, "newest session-log entry must match 'Last updated'"


def test_roadmap_exists_and_rules_reference_memory_files():
    assert "Rejected" in _read("ROADMAP.md")
    assert "## Memory files" in RULES.read_text(encoding="utf-8")
