"""Tests for the execution-rule corrections that property testing surfaced.

Each of these is a regression test for a bug the random-history generator found
and the original deterministic generator could not, because it only ever reworked
one operation backwards.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import execution as ex  # noqa: E402
import generate  # noqa: E402
import model  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    c = model.create(tmp_path / "t.db")
    generate.seed_definitions(c)
    generate.seed_lots(c)
    c.execute("INSERT INTO work_order VALUES ('WO-T','BRKT-100',5,'IN_PROCESS','t')")
    c.execute("INSERT INTO unit (unit_id, wo_id, serial, lot_qty, status, "
              "current_seq) VALUES ('U1','WO-T','SN1',NULL,'IN_PROCESS',NULL)")
    c.commit()
    return c


def _run_to(conn, uid, upto):
    wc = {10: "WC-CUT", 20: "WC-WELD", 30: "WC-MACH", 40: "WC-PAINT", 50: "WC-INSP"}
    op = {10: "OP-01", 20: "OP-02", 30: "OP-03", 40: "OP-01", 50: "OP-03"}
    for seq in (10, 20, 30, 40, 50):
        if seq > upto:
            break
        ex.start_operation(conn, uid, seq, op[seq], wc[seq])
        if seq == 10:
            ex.issue_material(conn, uid, 10, "L-5100", 1.0, op[seq])
        elif seq == 20:
            ex.issue_material(conn, uid, 20, "L-7001", 0.4, op[seq])
            ex.issue_material(conn, uid, 20, "L-7002", 2.0, op[seq])
        elif seq == 40:
            ex.issue_material(conn, uid, 40, "L-7003", 0.15, op[seq])
        ex.complete_operation(conn, uid, seq, op[seq], wc[seq])
    conn.commit()


def test_rework_two_steps_back_allows_reissue_and_recompletion(conn):
    """A rework re-entry at op N restarts the pass for EVERY op from N onward.

    The bug: `passes` and the completion boundary were scoped to `seq = this op`,
    so a unit reworked from op 40 back to op 10 was refused at op 20 for a 200%
    over-issue that was entirely legitimate, and refused again at op 30 for a
    "second completion without an intervening rework entry". Both refusals were
    wrong, and the original generator never produced them because it only ever
    reworked one step back.
    """
    _run_to(conn, "U1", 40)
    ncr = ex.raise_ncr(conn, "U1", 40, "found at final audit")
    ex.disposition_ncr(conn, ncr, "REWORK", "OP-03", rework_to_seq=10)

    # The whole route must be runnable again, consuming materials again.
    _run_to(conn, "U1", 50)

    rows = conn.execute(
        "SELECT seq, COUNT(*) n FROM op_record WHERE unit_id='U1' "
        "AND action='COMPLETE' GROUP BY seq").fetchall()
    completes = {r["seq"]: r["n"] for r in rows}
    assert completes[10] == 2, completes
    assert completes[20] == 2, completes

    issued = conn.execute(
        "SELECT COALESCE(SUM(qty),0) q FROM consumption WHERE unit_id='U1' "
        "AND component='WELD-WIRE'").fetchone()["q"]
    assert issued == pytest.approx(0.8), "second pass must consume weld wire again"


def test_over_issue_still_refused_within_a_single_pass(conn):
    """The pass-aware budget must not become no budget."""
    ex.start_operation(conn, "U1", 10, "OP-01", "WC-CUT")
    ex.issue_material(conn, "U1", 10, "L-5100", 1.0, "OP-01")
    with pytest.raises(ex.IssueError, match="over-issue"):
        ex.issue_material(conn, "U1", 10, "L-5100", 1.0, "OP-01")


def test_partial_scrap_does_not_scrap_the_whole_batch(conn):
    """Lot model: scrapping 25 of 400 leaves the batch runnable.

    The bug this covers: `scrap()` set status='SCRAPPED' unconditionally, so a
    partial scrap inside a lot-tracked batch killed the batch and the next
    operation refused to start. It only surfaced when the lot-tracked product was
    finally exercised.
    """
    conn.execute("INSERT INTO work_order VALUES ('WO-L','PLATE-200',400,"
                 "'IN_PROCESS','t')")
    conn.execute("INSERT INTO unit (unit_id, wo_id, serial, lot_qty, status, "
                 "current_seq) VALUES ('B1','WO-L',NULL,400,'IN_PROCESS',NULL)")
    conn.commit()

    ex.start_operation(conn, "B1", 10, "OP-02", "WC-CUT")
    ex.issue_material(conn, "B1", 10, "L-8001", 400.0, "OP-02")
    ex.complete_operation(conn, "B1", 10, "OP-02", "WC-CUT")
    ex.start_operation(conn, "B1", 20, "OP-03", "WC-MACH")
    ex.scrap(conn, "B1", 20, "OP-03", "WC-MACH", "drill breakout", qty=25.0)

    status = conn.execute("SELECT status FROM unit WHERE unit_id='B1'").fetchone()
    assert status["status"] == "IN_PROCESS", "partial scrap must not kill the batch"
    ex.complete_operation(conn, "B1", 20, "OP-03", "WC-MACH", qty=375.0)
    ex.start_operation(conn, "B1", 30, "OP-04", "WC-INSP")


def test_scrapping_the_remainder_does_scrap_the_batch(conn):
    conn.execute("INSERT INTO work_order VALUES ('WO-L','PLATE-200',100,"
                 "'IN_PROCESS','t')")
    conn.execute("INSERT INTO unit (unit_id, wo_id, serial, lot_qty, status, "
                 "current_seq) VALUES ('B2','WO-L',NULL,100,'IN_PROCESS',NULL)")
    conn.commit()
    ex.start_operation(conn, "B2", 10, "OP-02", "WC-CUT")
    ex.issue_material(conn, "B2", 10, "L-8001", 100.0, "OP-02")
    ex.scrap(conn, "B2", 10, "OP-02", "WC-CUT", "coil defect", qty=40.0)
    ex.scrap(conn, "B2", 10, "OP-02", "WC-CUT", "coil defect", qty=60.0)
    status = conn.execute("SELECT status FROM unit WHERE unit_id='B2'").fetchone()
    assert status["status"] == "SCRAPPED"

    with pytest.raises(ex.ConservationError, match="only"):
        ex.scrap(conn, "B2", 10, "OP-02", "WC-CUT", "again", qty=1.0)


def test_ncr_after_completion_does_not_double_count(conn):
    """An NCR consumes a START only if it TERMINATED the pass.

    START -> NCR              the pass ended undone; the NCR balances the start
    START -> COMPLETE -> NCR  the pass completed; counting the NCR again would
                              produce negative in-process, which is the ledger
                              claiming more work left an operation than entered.
    """
    _run_to(conn, "U1", 50)
    ex.raise_ncr(conn, "U1", 50, "found on audit after completion")
    conn.commit()
    rows = ex.conservation_report(conn, "WO-T")
    assert all(not r["violates"] for r in rows), rows
    op50 = [r for r in rows if r["seq"] == 50][0]
    assert op50["in_process"] == pytest.approx(0.0)
    assert op50["nonconformances"] == 0, "a post-completion NCR must not consume a start"
