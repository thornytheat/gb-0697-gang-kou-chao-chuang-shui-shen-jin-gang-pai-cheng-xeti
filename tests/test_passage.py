import shutil
from pathlib import Path

import pytest
from decimal import Decimal

from app.pipeline import run, sample_text
from app.config import DATA_DIR, CHANGEOVER_SECONDS


def csv_text(rows):
    return "\n".join(rows) + "\n"


TIDES = [("00:00", 1), ("01:00", 1), ("02:00", 3), ("03:00", 3),
         ("04:00", 1), ("05:00", 3), ("06:00", 3), ("07:00", 1)]


def tide_header():
    return ["id,kind,start,end,value,aux,version,unit,direction"]


def tide_rows():
    return [f"T{i:02d},tide,2026-11-18T{t}:00Z,,{v},,1,m,"
            for i, (t, v) in enumerate(TIDES)]


def by_id(result):
    return {row["id"]: row for row in result["assigned"]}


def num(value):
    return Decimal(str(value))


def test_sample_has_both_directions(result):
    directions = {row["direction"] for row in result["assigned"]}
    assert directions == {"up", "down"}


def test_single_direction_at_any_moment(result):
    for span in result["lane_spans"]:
        assert span["exclusive"], span


def test_no_lane_conflicts(result):
    assert result["lane_conflicts"] == []


def test_every_changeover_starts_after_previous_direction_cleared(result):
    bars = result["direction_occupancy"]
    for changeover in [b for b in bars if b["kind"] == "CHANGEOVER"]:
        previous = [b for b in bars if b["kind"] == "PASSAGE"
                    and num(b["end_s"]) <= num(changeover["start_s"])]
        assert previous, "换向清空之前必须已有本向航次结束"
        assert max(num(b["end_s"]) for b in previous) == num(changeover["start_s"])


def test_changeover_occupies_channel(result):
    for changeover in result["changeovers"]:
        assert num(changeover["end_s"]) - num(changeover["start_s"]) == CHANGEOVER_SECONDS
        assert changeover["preparation"]
        assert changeover["status"] == "CLEARED"


def test_directions_never_overlap_directly(result):
    bars = sorted(result["direction_occupancy"], key=lambda b: num(b["start_s"]))
    for left, right in zip(bars, bars[1:]):
        if left["direction"] != right["direction"]:
            assert num(left["end_s"]) <= num(right["start_s"])
            assert right["direction"] == "changeover" or left["direction"] == "changeover"


def test_waits_name_a_position(result):
    for row in result["assigned"]:
        if row["status"] == "SCHEDULED":
            assert row["wait_position"]
            assert num(row["wait_s"]) >= 0


def test_urgent_upstream_goes_before_finishing_downstream():
    """赶潮重载上行船应插在下行编队之间，而不是等所有下行船走完。"""
    rows = (tide_header() + tide_rows() + [
        "DN1,ship,2026-11-18T00:00:00Z,2026-11-18T12:00:00Z,9,30,1,m,down",
        "DN2,ship,2026-11-18T00:00:00Z,2026-11-18T12:00:00Z,9,30,1,m,down",
        "UP1,ship,2026-11-18T00:00:00Z,2026-11-18T04:00:00Z,9.4,60,1,m,up",
        "P1,pilot,2026-11-18T00:00:00Z,2026-11-19T00:00:00Z,0,,1,m,",
    ])
    result = run(csv_text(rows))
    ships = by_id(result)
    assert ships["UP1"]["status"] == "SCHEDULED"
    assert num(ships["UP1"]["window_slack_s"]) >= 0
    assert num(ships["UP1"]["start_s"]) < num(ships["DN2"]["start_s"])


def test_downstream_in_section_is_cleared_first():
    """已进入窄段的下行船先清空，不能让赶潮上行船把它扣在段内。"""
    rows = (tide_header() + tide_rows() + [
        "DN1,ship,2026-11-17T23:00:00Z,2026-11-18T12:00:00Z,9,30,1,m,down",
        "UP1,ship,2026-11-18T00:00:00Z,2026-11-18T04:00:00Z,9.4,60,1,m,up",
        "P1,pilot,2026-11-17T20:00:00Z,2026-11-19T00:00:00Z,0,,1,m,",
    ])
    result = run(csv_text(rows))
    ships = by_id(result)
    assert num(ships["DN1"]["start_s"]) < num(ships["UP1"]["start_s"])
    assert num(ships["DN1"]["start_s"]) == num(ships["DN1"]["window_open_s"])


def test_missing_window_is_reported_not_dropped():
    rows = (tide_header() + tide_rows() + [
        "UP1,ship,2026-11-18T00:00:00Z,2026-11-18T01:00:00Z,10,30,1,m,up",
        "P1,pilot,2026-11-18T00:00:00Z,2026-11-19T00:00:00Z,0,,1,m,",
    ])
    result = run(csv_text(rows))
    row = by_id(result)["UP1"]
    assert row["status"] == "NO_SAFE_SLOT" and row["reason"] == "NO_SAFE_WINDOW"
    assert result["blocked"] is True


def test_legacy_eight_column_plan_still_runs():
    text = sample_text()
    legacy = "\n".join(
        ",".join(line.split(",")[:8]) if index and not line.startswith("id,") else line
        for index, line in enumerate(text.splitlines()))
    result = run(legacy)
    assert len(result["assigned"]) == len(run(text)["assigned"])
    assert all(row["direction"] == "up" for row in result["assigned"])


def test_direction_aliases():
    from app.modules.m2_vessels.rules import ship_direction
    from app.errors import BusinessError
    assert ship_direction("进港") == "up" and ship_direction("UP") == "up"
    assert ship_direction("出港") == "down" and ship_direction("下行") == "down"
    assert ship_direction("") == "up"
    with pytest.raises(BusinessError):
        ship_direction("横向")


def test_invalid_direction_is_quarantined():
    rows = (tide_header() + tide_rows() + [
        "S1,ship,2026-11-18T00:00:00Z,2026-11-18T12:00:00Z,9,30,1,m,横向",
        "P1,pilot,2026-11-18T00:00:00Z,2026-11-19T00:00:00Z,0,,1,m,",
    ])
    result = run(csv_text(rows))
    assert any(row["reason"] == "DIRECTION" for row in result["quarantine"])


def test_search_stays_within_budget():
    rows = tide_header()
    for hour in range(24):
        rows.append(f"T{hour:02d},tide,2026-11-18T{hour:02d}:00:00Z,,"
                    f"{3 if hour % 8 in (2, 3, 4, 5) else 1},,1,m,")
    for index in range(1, 11):
        direction = "up" if index % 3 else "down"
        rows.append(f"S{index:02d},ship,2026-11-18T00:{index:02d}:00Z,"
                    f"2026-11-19T00:00:00Z,{9 if index % 2 else 8.5},30,1,m,{direction}")
    rows.append("P1,pilot,2026-11-18T00:00:00Z,2026-11-19T00:00:00Z,0,,1,m,")
    result = run(csv_text(rows))
    assert len(result["assigned"]) == 10
    assert result["lane_conflicts"] == []


@pytest.fixture
def fresh_store():
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    from app import store
    yield store
    shutil.rmtree(DATA_DIR, ignore_errors=True)


def test_rerun_reuses_batch_and_slots(fresh_store):
    text = sample_text()
    first = fresh_store.save(text, run(text))
    slots = fresh_store.occupancy(first["id"])
    second = fresh_store.save(text, run(text))
    assert first["id"] == second["id"]
    assert fresh_store.occupancy(first["id"]) == slots


def test_restore_returns_old_sequence_without_duplicate_slots(fresh_store):
    text = sample_text()
    first = fresh_store.save(text, run(text))
    before = fresh_store.occupancy(first["id"])
    reviewed = fresh_store.review(first["id"], 0, "退回")
    assert reviewed["revision"] == 1
    restored = fresh_store.restore(first["id"], 0)
    assert restored["revision"] == 2 and restored["decision"] == "恢复旧版"
    assert fresh_store.occupancy(first["id"]) == before
    again = fresh_store.restore(first["id"], 0)
    assert again["revision"] == 3
    assert fresh_store.occupancy(first["id"]) == before
    assert [v["revision"] for v in fresh_store.versions(first["id"])] == [3, 2, 1, 0]


def test_restore_rejects_future_revision(fresh_store):
    from app.errors import BusinessError
    first = fresh_store.save(sample_text(), run(sample_text()))
    with pytest.raises(BusinessError) as caught:
        fresh_store.restore(first["id"], 5)
    assert caught.value.status == 409


def test_restore_rejects_bool_revision(fresh_store):
    from app.errors import BusinessError
    first = fresh_store.save(sample_text(), run(sample_text()))
    with pytest.raises(BusinessError) as caught:
        fresh_store.restore(first["id"], True)
    assert caught.value.code == "REVISION"


def test_legacy_batch_renders_after_backfill(fresh_store):
    """旧版批次没有双向字段时补空，工作台仍可打开。"""
    import json
    from fastapi.testclient import TestClient
    result = {
        "raw": [], "effective": [], "tide_windows": [], "channel_windows": [],
        "assigned": [{"id": "S1", "start_s": 0, "end_s": 100,
                      "status": "SCHEDULED", "line": 2}],
        "checked": [{"id": "S1", "start_s": 0, "end_s": 100,
                     "status": "SCHEDULED", "line": 2, "check": "CLEAR"}],
        "final": [{"id": "S1", "start_s": 0, "end_s": 100, "status": "SCHEDULED",
                   "line": 2, "check": "CLEAR", "start": "01-01 00:00 UTC",
                   "end": "01-01 00:02 UTC"}],
        "conflicts": [], "quarantine": [], "reasons": {}, "blocked": False,
        "change_log": [], "reports": [], "rule_version": "2026.11.18.2",
        "balance": {"input": 1, "accepted": 1, "quarantined": 0, "blank": 0},
    }
    with fresh_store.connect() as db:
        db.execute("INSERT INTO runs(id,digest,result,created) VALUES(?,?,?,?)",
                   ("legacy0000000001", "d", json.dumps(result), "2026-01-01T00:00:00+00:00"))
    from app.main import app
    page = TestClient(app, raise_server_exceptions=False).get("/?run_id=legacy0000000001")
    assert page.status_code == 200
    assert "无航道占用" in page.text


def test_restore_keeps_old_rule_version(fresh_store):
    first = fresh_store.save(sample_text(), run(sample_text()))
    fresh_store.review(first["id"], 0, "退回")
    restored = fresh_store.restore(first["id"], 0)
    assert restored["rule_version"] == fresh_store.get(first["id"])["rule_version"]
