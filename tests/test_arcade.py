import re
from pathlib import Path

import pytest

from eventmate.arcade_pulse import STAGES, ArcadePulse
from eventmate.service import EventMate
from eventmate.web.app import STATIC_FILES, create_app
from tests.conftest import EXAMPLE, TODAY
from tests.test_web import post

STATIC = Path(__file__).resolve().parent.parent / "eventmate/web/static"


@pytest.fixture
def client(tmp_path):
    em = EventMate(log_dir=tmp_path / "logs", plan_dir=tmp_path / "plans", run_id="arcade")
    return create_app(em, today_fn=lambda: TODAY).test_client()


def states(snap):
    return {s["name"]: s["state"] for s in snap["stages"]}


def test_pulse_idle_until_a_run_begins():
    p = ArcadePulse()
    p.stage_started("parse")  # hook outside a run is a no-op
    snap = p.snapshot()
    assert snap["state"] == "idle" and snap["elapsed_ms"] is None
    assert [s["name"] for s in snap["stages"]] == list(STAGES)
    assert set(states(snap).values()) == {"waiting"}


def test_pulse_only_lights_stages_the_pipeline_reports():
    p = ArcadePulse()
    p.begin_run("x")
    p.stage_started("parse")
    p.stage_done("parse")
    p.stage_started("budget")
    snap = p.snapshot()
    assert states(snap)["parse"] == "done" and states(snap)["budget"] == "active"
    assert states(snap)["schedule"] == "waiting"
    p.stage_started("nonsense")  # unknown stage names are ignored
    p.finish(True)
    snap = p.snapshot()
    assert snap["state"] == "done" and states(snap)["budget"] == "done" and states(snap)["schedule"] == "waiting"


def test_pulse_failure_marks_active_stage_and_truncates_text():
    p = ArcadePulse()
    p.begin_run("y" * 500)
    p.stage_started("select")
    p.finish(False, "e" * 1000)
    snap = p.snapshot()
    assert snap["state"] == "failed" and states(snap)["select"] == "failed"
    assert len(snap["label"]) == 120 and len(snap["error"]) == 300
    p.stage_started("validate")  # nothing moves after a run has ended
    assert states(p.snapshot())["validate"] == "waiting"


def test_pulse_endpoint_is_read_only_json(client):
    r = client.get("/api/arcade/pulse")
    assert r.status_code == 200 and r.is_json
    assert [s["name"] for s in r.get_json()["stages"]] == list(STAGES)
    assert client.post("/api/arcade/pulse").status_code in (403, 405)


def test_plan_run_drives_the_pulse(client):
    assert post(client, "/api/plans", {"request": EXAMPLE}).status_code == 201
    snap = client.get("/api/arcade/pulse").get_json()
    assert snap["state"] == "done" and snap["label"].startswith("plan: ")
    assert set(states(snap).values()) == {"done"}


def test_arcade_page_and_assets_are_served_with_credit(client):
    page = client.get("/arcade")
    assert page.status_code == 200 and page.mimetype == "text/html"
    html = page.get_data(as_text=True)
    assert "Snji" in html and "dantheman" in html and "CC BY 4.0" in html
    for name in ("cabinet.glb", "arcade.js", "arcade3d.js", "three.min.js"):
        assert client.get(f"/static/{name}").status_code == 200, name
    assert client.get("/static/character.glb").status_code == 404


def test_every_whitelisted_static_file_exists_and_page_scripts_are_local():
    for name in STATIC_FILES:
        assert (STATIC / name).is_file(), name
    html = (STATIC / "arcade.html").read_text()
    for src in re.findall(r'<script[^>]+src="([^"]+)"', html):
        assert src.startswith("/static/") and src.rsplit("/", 1)[-1] in STATIC_FILES, src
