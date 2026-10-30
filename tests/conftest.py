from datetime import date

import pytest

from eventmate.decision_logger import DecisionLogger
from eventmate.agents.vendor import VendorRepository

TODAY = date(2026, 10, 30)
EXAMPLE = (
    "Plan a 120-person engagement party in Pune on December 12 under INR 200,000. "
    "Vegetarian catering, indoor venue, photographer, and reminders for "
    "invitations and vendor confirmation."
)


@pytest.fixture(autouse=True)
def _no_real_env_file(monkeypatch):
    """A developer's real .env must never change test behaviour or reach the network."""
    monkeypatch.setenv("EVENTMATE_ENV_FILE", "/nonexistent/eventmate.env")
    for name in ("NVIDIA_API_KEY", "OPENROUTER_API_KEY", "BRAVE_SEARCH_API_KEY", "GOOGLE_PLACES_API_KEY", "OPENWEATHER_API_KEY"):
        monkeypatch.delenv(name, raising=False)  # no test may spend a credit or call a keyed service


@pytest.fixture
def today():
    return TODAY


@pytest.fixture
def repo():
    return VendorRepository()


@pytest.fixture
def logger(tmp_path):
    return DecisionLogger(log_dir=tmp_path, run_id="test-run")


@pytest.fixture
def logs_tool(tmp_path):
    from eventmate.logs import ToolCallLog

    return ToolCallLog(log_dir=tmp_path, run_id="tools")


@pytest.fixture
def intake(repo, logger):
    from eventmate.agents.manager import ManagerAgent

    return ManagerAgent(repo=repo, logger=logger).run_intake(EXAMPLE, today=TODAY)


def csrf_get(client, url):
    """A GET the way the UI sends it: with the CSRF header that quota-spending routes require."""
    return client.get(url, headers={"X-EventMate-CSRF": client.get("/api/session").get_json()["csrf"]})
