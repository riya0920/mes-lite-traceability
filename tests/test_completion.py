"""Tests for the third-pass modules."""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import scheduling as SCH  # noqa: E402
import signature as SIG  # noqa: E402


# ---------------------------------------------------------------------------
# shift calendar
# ---------------------------------------------------------------------------

def test_work_does_not_happen_outside_a_shift():
    cal = SCH.ShiftCalendar()
    # 02:00 on a Monday is before first shift; work starts at 06:00.
    assert cal.next_working_minute(2 * 60) == 6 * 60


def test_an_eight_hour_job_started_at_1400_does_not_finish_at_2200():
    """The calendar is not decoration."""
    cal = SCH.ShiftCalendar()
    r = cal.elapsed_vs_working(14 * 60, 8 * 60)
    assert r["elapsed_minutes"] > 8 * 60
    assert r["calendar_inflation"] > 1.0


def test_breaks_are_subtracted_from_working_time():
    cal = SCH.ShiftCalendar()
    # 06:00 + 5 h of work crosses the 10:00-10:20 break.
    end = cal.add_working_minutes(6 * 60, 5 * 60)
    assert end == pytest.approx(6 * 60 + 5 * 60 + 20)


def test_the_weekend_is_skipped():
    cal = SCH.ShiftCalendar()
    saturday = 5 * 1440 + 8 * 60
    assert cal.next_working_minute(saturday) >= 7 * 1440


# ---------------------------------------------------------------------------
# finite capacity
# ---------------------------------------------------------------------------

def _jobs(n=6):
    return [SCH.Job(f"J{i}", [(10, "A", 60.0), (20, "B", 30.0)],
                    due=500.0, released=0.0) for i in range(n)]


def test_one_machine_cannot_run_two_jobs_at_once():
    """The constraint an infinite-capacity plan quietly drops."""
    out = SCH.schedule(_jobs(3), {"A": 1, "B": 1}, "FIFO")
    intervals = []
    for ops in out["schedule"].values():
        for o in ops:
            if o["wc"] == "A":
                intervals.append((o["start"], o["end"]))
    intervals.sort()
    for (s1, e1), (s2, e2) in zip(intervals, intervals[1:]):
        assert s2 >= e1 - 1e-9, "two jobs overlapped on one machine"


def test_finite_capacity_is_never_faster_than_infinite():
    jobs = _jobs(8)
    fin = SCH.schedule(jobs, {"A": 1, "B": 1}, "FIFO")
    inf = SCH.infinite_capacity(jobs)
    assert fin["mean_flow_time"] >= inf["mean_flow_time"] - 1e-6
    assert fin["makespan"] >= inf["makespan"] - 1e-6


def test_more_machines_never_make_the_schedule_worse():
    jobs = _jobs(8)
    one = SCH.schedule(jobs, {"A": 1, "B": 1}, "FIFO")
    two = SCH.schedule(jobs, {"A": 2, "B": 2}, "FIFO")
    assert two["makespan"] <= one["makespan"] + 1e-6


def test_spt_beats_fifo_on_mean_flow_time():
    """SPT provably minimises mean flow time; that is why it exists."""
    jobs = [SCH.Job(f"J{i}", [(10, "A", float(m))], due=1e6)
            for i, m in enumerate([200, 10, 150, 20, 5, 300])]
    fifo = SCH.schedule(jobs, {"A": 1}, "FIFO")
    spt = SCH.schedule(jobs, {"A": 1}, "SPT")
    assert spt["mean_flow_time"] < fifo["mean_flow_time"]


def test_edd_beats_spt_on_maximum_lateness():
    jobs = [SCH.Job(f"J{i}", [(10, "A", float(m))], due=float(d))
            for i, (m, d) in enumerate([(100, 900), (20, 60), (80, 800),
                                        (30, 120), (10, 40)])]
    edd = SCH.schedule(jobs, {"A": 1}, "EDD")
    spt = SCH.schedule(jobs, {"A": 1}, "SPT")
    assert edd["max_lateness"] <= spt["max_lateness"]


def test_every_operation_is_scheduled_exactly_once():
    jobs = _jobs(5)
    out = SCH.schedule(jobs, {"A": 2, "B": 1}, "CR")
    for j in jobs:
        assert len(out["schedule"][j.job_id]) == len(j.ops)


def test_an_unknown_dispatch_rule_raises():
    with pytest.raises(ValueError):
        SCH.schedule(_jobs(2), {"A": 1, "B": 1}, "MAGIC")


# ---------------------------------------------------------------------------
# electronic signatures
# ---------------------------------------------------------------------------

def test_a_signature_must_state_its_meaning():
    """'OP-03 signed this' is worthless without knowing what they asserted."""
    with pytest.raises(ValueError, match="meaning"):
        SIG.sign({"a": 1}, "OP-03", "looks fine", b"k")


def test_altering_the_payload_breaks_the_signature():
    s = SIG.sign({"qty": 1}, "QE-1", "APPROVED", b"k")
    row = {**s, "payload": json.dumps({"qty": 999})}
    assert not SIG.verify(row, b"k")


def test_the_wrong_key_does_not_verify():
    s = SIG.sign({"qty": 1}, "QE-1", "APPROVED", b"k")
    assert SIG.verify(s, b"k")
    assert not SIG.verify(s, b"other")


def test_canonical_serialisation_is_key_order_independent():
    """A signature over a non-canonical form fails the first time somebody adds
    a field."""
    assert SIG.canonical({"b": 1, "a": 2}) == SIG.canonical({"a": 2, "b": 1})


def test_the_hash_chain_detects_an_edit_to_one_row(tmp_path):
    conn = sqlite3.connect(tmp_path / "e.db")
    conn.row_factory = sqlite3.Row
    log = SIG.SignatureLog(conn, b"key")
    for i in range(5):
        log.add("ncr", f"N{i}", {"i": i}, "QE-1", "APPROVED")
    assert log.verify_chain()["intact"]
    conn.execute("UPDATE esign SET payload='{\"i\":99}' WHERE sig_id=2")
    conn.commit()
    out = log.verify_chain()
    assert not out["intact"] and out["broken_at"] == 2


def test_deleting_a_row_breaks_the_chain(tmp_path):
    conn = sqlite3.connect(tmp_path / "e.db")
    conn.row_factory = sqlite3.Row
    log = SIG.SignatureLog(conn, b"key")
    for i in range(5):
        log.add("ncr", f"N{i}", {"i": i}, "QE-1", "APPROVED")
    conn.execute("DELETE FROM esign WHERE sig_id=3")
    conn.commit()
    assert not log.verify_chain()["intact"]


def test_retention_covers_every_record_type_with_a_stated_basis(tmp_path):
    conn = sqlite3.connect(tmp_path / "e.db")
    conn.row_factory = sqlite3.Row
    log = SIG.SignatureLog(conn, b"key")
    rows = log.retention_due(2026)
    assert {r["record_type"] for r in rows} >= {"op_record", "consumption", "esign"}
    for r in rows:
        assert r["years"] >= 5 and r["basis"]
