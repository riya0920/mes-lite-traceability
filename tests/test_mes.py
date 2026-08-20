"""SE-2 tests: the invariants and the refusals.

These are the tests that would catch a regression that matters. A work-order
tracker that stops enforcing precedence still looks like it works; the only thing
that notices is a test.
"""
from __future__ import annotations

import pathlib
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import execution as ex  # noqa: E402
import generate  # noqa: E402
import model  # noqa: E402
import trace  # noqa: E402


@pytest.fixture
def db(tmp_path):
    conn = model.create(tmp_path / "t.db")
    generate.seed_definitions(conn)
    generate.seed_lots(conn)
    return conn


def _unit(conn, wo="WO-T1", sku="BRKT-100", uid="U1"):
    conn.execute("INSERT OR IGNORE INTO work_order VALUES (?,?,?,?,?)",
                 (wo, sku, 1, "RELEASED", "2026-07-01T06:00:00+00:00"))
    conn.execute("INSERT INTO unit (unit_id, wo_id, serial, lot_qty, status, current_seq) "
                 "VALUES (?,?,?,NULL,'IN_PROCESS',NULL)", (uid, wo, uid))
    conn.commit()
    return uid


def test_precedence_blocks_out_of_order_start(db):
    u = _unit(db)
    with pytest.raises(ex.PrecedenceError):
        ex.start_operation(db, u, 30, "OP-01", "WC-MACH")


def test_certification_blocks_uncertified_operator(db):
    u = _unit(db)
    ex.start_operation(db, u, 10, "OP-01", "WC-CUT")
    ex.issue_material(db, u, 10, "L-5100", 1.0, "OP-01")
    ex.complete_operation(db, u, 10, "OP-01", "WC-CUT")
    with pytest.raises(ex.CertificationError):
        ex.start_operation(db, u, 20, "OP-05", "WC-WELD")   # OP-05 has no certs


def test_authorised_deviation_is_allowed_and_recorded(db):
    u = _unit(db)
    ex.start_operation(db, u, 10, "OP-01", "WC-CUT")
    ex.issue_material(db, u, 10, "L-5100", 1.0, "OP-01")
    ex.complete_operation(db, u, 10, "OP-01", "WC-CUT")
    ex.start_operation(db, u, 20, "OP-05", "WC-WELD", deviation_ref="DEV-1")
    row = db.execute(
        "SELECT deviation_ref FROM op_record WHERE unit_id=? AND seq=20", (u,)).fetchone()
    assert row["deviation_ref"] == "DEV-1"


def test_over_issue_is_refused_but_within_tolerance_is_allowed(db):
    u = _unit(db)
    ex.start_operation(db, u, 10, "OP-01", "WC-CUT")
    ex.issue_material(db, u, 10, "L-5100", 1.05, "OP-01")     # inside +10%
    with pytest.raises(ex.IssueError):
        ex.issue_material(db, u, 10, "L-5100", 2.0, "OP-01")  # cumulative over


def test_off_bom_component_is_refused(db):
    u = _unit(db)
    ex.start_operation(db, u, 10, "OP-01", "WC-CUT")
    with pytest.raises(ex.IssueError):
        ex.issue_material(db, u, 10, "L-7003", 0.15, "OP-01")  # powder at op 10


def test_double_completion_is_refused(db):
    u = _unit(db)
    ex.start_operation(db, u, 10, "OP-01", "WC-CUT")
    ex.issue_material(db, u, 10, "L-5100", 1.0, "OP-01")
    ex.complete_operation(db, u, 10, "OP-01", "WC-CUT")
    with pytest.raises(ex.ConservationError):
        ex.complete_operation(db, u, 10, "OP-01", "WC-CUT")


def test_rework_permits_a_second_pass_and_a_second_issue(db):
    """The hard part: an operation must be completable twice AFTER a rework entry,
    and the material budget must scale with the number of passes."""
    u = _unit(db)
    for seq, wc in [(10, "WC-CUT"), (20, "WC-WELD"), (30, "WC-MACH"), (40, "WC-PAINT")]:
        ex.start_operation(db, u, seq, "OP-01", wc)
        if seq == 10:
            ex.issue_material(db, u, 10, "L-5100", 1.0, "OP-01")
        if seq == 20:
            ex.issue_material(db, u, 20, "L-7001", 0.4, "OP-01")
            ex.issue_material(db, u, 20, "L-7002", 2.0, "OP-01")
        if seq == 40:
            ex.issue_material(db, u, 40, "L-7003", 0.15, "OP-01")
        ex.complete_operation(db, u, seq, "OP-01", wc)

    ncr = ex.raise_ncr(db, u, 40, "blemish")
    ex.disposition_ncr(db, ncr, "REWORK", "OP-03", rework_to_seq=40)

    # second pass through op 40: both the completion and the material must be legal
    ex.start_operation(db, u, 40, "OP-01", "WC-PAINT")
    ex.issue_material(db, u, 40, "L-7003", 0.15, "OP-01")
    ex.complete_operation(db, u, 40, "OP-01", "WC-PAINT")

    n = db.execute("SELECT COUNT(*) c FROM consumption WHERE unit_id=? AND seq=40",
                   (u,)).fetchone()["c"]
    assert n == 2


def test_conservation_balances_across_a_full_week():
    """Every (work order, operation) must balance after a realistic run."""
    conn = model.create(":memory:")
    generate.seed_definitions(conn)
    generate.seed_lots(conn)
    generate.run_week(conn, np.random.default_rng(4))
    rows = ex.conservation_report(conn)
    assert rows, "no operations recorded"

    # The invariant is one-sided: you can never complete or scrap more than you
    # started. A POSITIVE in-process balance is legitimate -- it is work still on
    # the floor. The planted-violation work order deliberately leaves units mid
    # operation, so asserting in_process == 0 everywhere would be asserting that
    # the plant is empty, which is not the invariant.
    for r in rows:
        assert not r["violates"], r
        assert r["in_process"] >= -1e-9, r
    ex.assert_conservation(conn)

    # The production work orders DO run to completion, so those must balance to
    # zero -- every unit is finished, scrapped, or dispositioned.
    for wo in ("WO-1001", "WO-1002", "WO-1003"):
        for r in ex.conservation_report(conn, wo):
            assert r["in_process"] == pytest.approx(0.0, abs=1e-9), r


def test_recall_finds_everything_through_a_lot_split():
    conn = model.create(":memory:")
    generate.seed_definitions(conn)
    generate.seed_lots(conn)
    truth = generate.run_week(conn, np.random.default_rng(4))

    drill = trace.recall_drill(conn, "L-4471")
    found = set(drill["shipped"] + drill["finished_on_hand"] + drill["in_process"]
                + drill["already_scrapped"])
    expected = set(truth["recall_expected_units"])
    assert found == expected
    assert not (found & set(truth["recall_must_exclude"]))

    # The naive query -- the one that stops at the named lot -- must do WORSE,
    # which is the whole point of the split-tree walk.
    naive = {r["unit_id"] for r in conn.execute(
        "SELECT DISTINCT unit_id FROM consumption WHERE lot_id='L-4471'")}
    assert naive < found


def test_affected_lots_walks_up_and_down_the_split_tree():
    conn = model.create(":memory:")
    generate.seed_definitions(conn)
    generate.seed_lots(conn)
    ex.split_lot(conn, "L-4471", "L-A", 100)
    ex.split_lot(conn, "L-A", "L-A1", 40)
    ex.split_lot(conn, "L-4471", "L-B", 50)
    # Naming a grandchild must still pull in the root and the sibling branch.
    assert trace.affected_lots(conn, "L-A1") == {"L-4471", "L-A", "L-A1", "L-B"}
