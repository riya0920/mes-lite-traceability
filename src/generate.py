"""Plant activity generator: realistic weeks of execution, with planted violations.

The planted violations are the point. Every enforcement rule in execution.py is
attempted against here, so "the system blocks over-issues" is a count out of a
known denominator rather than a claim. Ground truth is returned alongside so the
runner can score enforcement N/N.

It also plants the RECALL SCENARIO deliberately: lot L-4471 is split, issued to two
work orders, and some of its units are scrapped, completed, and shipped. That is
the scenario the drill has to answer, and knowing the answer in advance is what
makes the drill verifiable rather than impressive.
"""
from __future__ import annotations

import datetime as dt

import numpy as np

import execution as ex

T0 = dt.datetime(2026, 7, 1, 6, 0, tzinfo=dt.timezone.utc)


def _ts(minutes: float) -> dt.datetime:
    return T0 + dt.timedelta(minutes=minutes)


def seed_definitions(conn) -> None:
    conn.executemany("INSERT OR IGNORE INTO work_center VALUES (?,?,?)", [
        ("WC-CUT", "Laser cutting", 2),
        ("WC-WELD", "Robotic weld cell", 2),
        ("WC-MACH", "CNC machining", 3),
        ("WC-PAINT", "Powder coat line", 1),
        ("WC-INSP", "Final inspection", 2),
    ])
    conn.executemany("INSERT OR IGNORE INTO product VALUES (?,?,?)", [
        ("BRKT-100", "Structural bracket, serialised", "serial"),
        ("PLATE-200", "Base plate, lot tracked", "lot"),
    ])
    # BRKT-100: cut -> weld -> machine -> paint -> inspect
    conn.executemany("INSERT OR IGNORE INTO operation VALUES (?,?,?,?,?,?,?)", [
        ("BRKT-100", 10, "Cut blank", "WC-CUT", 600, 95, None),
        ("BRKT-100", 20, "Weld assembly", "WC-WELD", 900, 180, "WELD-2"),
        ("BRKT-100", 30, "Machine faces", "WC-MACH", 1200, 240, "CNC-1"),
        ("BRKT-100", 40, "Powder coat", "WC-PAINT", 1800, 60, None),
        ("BRKT-100", 50, "Final inspect", "WC-INSP", 0, 120, "INSP-1"),
        ("PLATE-200", 10, "Shear", "WC-CUT", 400, 40, None),
        ("PLATE-200", 20, "Drill", "WC-MACH", 700, 70, "CNC-1"),
        ("PLATE-200", 30, "Inspect", "WC-INSP", 0, 45, "INSP-1"),
    ])
    conn.executemany("INSERT OR IGNORE INTO bom_line VALUES (?,?,?,?)", [
        ("BRKT-100", 10, "STEEL-BAR", 1.0),
        ("BRKT-100", 20, "WELD-WIRE", 0.4),
        ("BRKT-100", 20, "GUSSET", 2.0),
        ("BRKT-100", 40, "POWDER", 0.15),
        ("PLATE-200", 10, "STEEL-SHEET", 1.0),
    ])
    conn.executemany("INSERT OR IGNORE INTO operator VALUES (?,?)", [
        ("OP-01", "A. Nowak"), ("OP-02", "R. Silva"), ("OP-03", "M. Chen"),
        ("OP-04", "T. Okafor"), ("OP-05", "J. Dubois"),
    ])
    conn.executemany("INSERT OR IGNORE INTO certification VALUES (?,?)", [
        ("OP-01", "WELD-2"), ("OP-01", "CNC-1"),
        ("OP-02", "WELD-2"),
        ("OP-03", "CNC-1"), ("OP-03", "INSP-1"),
        ("OP-04", "INSP-1"),
        # OP-05 is deliberately uncertified for everything.
    ])
    conn.commit()


def seed_lots(conn) -> None:
    lots = [
        ("L-4471", "STEEL-BAR", "Meridian Steel", 400),
        ("L-4998", "STEEL-BAR", "Meridian Steel", 400),
        ("L-5100", "STEEL-BAR", "Delta Metals", 400),
        ("L-7001", "WELD-WIRE", "Arcline", 500),
        ("L-7002", "GUSSET", "Peninsula Fab", 2000),
        ("L-7003", "POWDER", "ChromaCoat", 300),
        ("L-8001", "STEEL-SHEET", "Meridian Steel", 900),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO lot (lot_id, component, supplier, qty_received, "
        "qty_on_hand, parent_lot, received_at) VALUES (?,?,?,?,?,NULL,?)",
        [(a, b, c, d, d, T0.isoformat()) for a, b, c, d in lots])
    conn.commit()


def run_week(conn, rng: np.random.Generator) -> dict:
    """Execute several work orders, with planted rule violations attempted.

    THE RECALL SCENARIO (ground truth, known in advance):
      L-4471 is split into L-4471-A (180) and L-4471-B (120).
      WO-1001 (BRKT-100, 12 serialised units) consumes L-4471-A at op 10.
      WO-1002 (BRKT-100, 8 units) consumes L-4471-B at op 10.
      WO-1003 (BRKT-100, 10 units) consumes clean L-4998 -- the control group.
    So the recall must find exactly the 20 units of WO-1001 + WO-1002 and none of
    WO-1003, through the split, which is the part a naive query misses.
    """
    truth: dict = {"blocked": {}, "attempted": {}}
    ex.split_lot(conn, "L-4471", "L-4471-A", 180, ts=_ts(0))
    ex.split_lot(conn, "L-4471", "L-4471-B", 120, ts=_ts(1))

    plan = [("WO-1001", "BRKT-100", 12, "L-4471-A"),
            ("WO-1002", "BRKT-100", 8, "L-4471-B"),
            ("WO-1003", "BRKT-100", 10, "L-4998")]
    minute = 10.0
    all_units: dict[str, list[str]] = {}

    for wo, sku, qty, steel_lot in plan:
        conn.execute("INSERT INTO work_order VALUES (?,?,?,?,?)",
                     (wo, sku, qty, "IN_PROCESS", _ts(minute).isoformat()))
        units = []
        for i in range(qty):
            uid = f"{wo}-U{i+1:03d}"
            conn.execute(
                "INSERT INTO unit (unit_id, wo_id, serial, lot_qty, status, current_seq) "
                "VALUES (?,?,?,NULL,'IN_PROCESS',NULL)",
                (uid, wo, f"SN{wo[-4:]}{i+1:03d}"))
            units.append(uid)
        all_units[wo] = units
        conn.commit()

        for u_idx, uid in enumerate(units):
            # PLANTED, not random. Rework re-entry is the hard part of an MES and
            # the first version left it to a 12% coin flip -- which came up tails
            # 27 times in a row and shipped a run in which the rework path was
            # never executed at all. Ground truth you cannot count is not ground
            # truth; these are deterministic so the expected counts are known.
            do_scrap = (u_idx % 11) == 5
            do_rework = (u_idx % 7) == 3
            for seq, wc, cert in [(10, "WC-CUT", None), (20, "WC-WELD", "WELD-2"),
                                  (30, "WC-MACH", "CNC-1"), (40, "WC-PAINT", None),
                                  (50, "WC-INSP", "INSP-1")]:
                op = _pick_operator(rng, cert)
                minute += rng.uniform(3, 9)
                ex.start_operation(conn, uid, seq, op, wc, ts=_ts(minute))
                if seq == 10:
                    ex.issue_material(conn, uid, 10, steel_lot, 1.0, op, ts=_ts(minute))
                elif seq == 20:
                    ex.issue_material(conn, uid, 20, "L-7001", 0.4, op, ts=_ts(minute))
                    ex.issue_material(conn, uid, 20, "L-7002", 2.0, op, ts=_ts(minute))
                elif seq == 40:
                    ex.issue_material(conn, uid, 40, "L-7003", 0.15, op, ts=_ts(minute))

                # Scrap a few units at op 30, and rework a few at op 50.
                if seq == 30 and do_scrap:
                    ex.scrap(conn, uid, 30, op, wc, "dimensional out of tolerance",
                             ts=_ts(minute))
                    break
                if seq == 50 and do_rework:
                    ncr = ex.raise_ncr(conn, uid, 50, "coating blemish", ts=_ts(minute))
                    ex.disposition_ncr(conn, ncr, "REWORK", "OP-03", rework_to_seq=40,
                                       ts=_ts(minute))
                    # Rework: back through 40 then 50 again.
                    minute += rng.uniform(5, 12)
                    ex.start_operation(conn, uid, 40, "OP-01", "WC-PAINT", ts=_ts(minute))
                    ex.issue_material(conn, uid, 40, "L-7003", 0.15, "OP-01", ts=_ts(minute))
                    ex.complete_operation(conn, uid, 40, "OP-01", "WC-PAINT", ts=_ts(minute))
                    minute += rng.uniform(2, 5)
                    ex.start_operation(conn, uid, 50, "OP-03", "WC-INSP", ts=_ts(minute))
                    ex.complete_operation(conn, uid, 50, "OP-03", "WC-INSP", ts=_ts(minute))
                    break
                ex.complete_operation(conn, uid, seq, op, wc, ts=_ts(minute))
        conn.execute("UPDATE work_order SET status='CLOSED' WHERE wo_id=?", (wo,))
        conn.commit()

    # ---------------- planted violations, all of which must be refused -------
    truth["attempted"], truth["blocked"] = _attempt_violations(conn, all_units)

    # ---------------- ship some finished goods ------------------------------
    conn.execute("INSERT INTO shipment VALUES ('SH-9001','Northwind Rail',?)",
                 (_ts(minute + 60).isoformat(),))
    conn.execute("INSERT INTO shipment VALUES ('SH-9002','Cascade Transit',?)",
                 (_ts(minute + 90).isoformat(),))
    shipped = []
    for wo, ship in (("WO-1001", "SH-9001"), ("WO-1002", "SH-9002")):
        done = conn.execute(
            "SELECT unit_id FROM unit WHERE wo_id=? AND status='COMPLETE' LIMIT 5",
            (wo,)).fetchall()
        for r in done:
            conn.execute("INSERT INTO shipment_line VALUES (?,?)", (ship, r["unit_id"]))
            shipped.append(r["unit_id"])
    conn.commit()

    truth["planted"] = {
        "scrapped_units": sorted(u for us in all_units.values()
                                 for i, u in enumerate(us) if i % 11 == 5),
        "reworked_units": sorted(u for us in all_units.values()
                                 for i, u in enumerate(us) if i % 7 == 3 and i % 11 != 5),
    }
    truth["work_orders"] = {wo: units for wo, units in all_units.items()}
    truth["recall_expected_units"] = sorted(all_units["WO-1001"] + all_units["WO-1002"])
    truth["recall_must_exclude"] = sorted(all_units["WO-1003"])
    truth["shipped_units"] = sorted(shipped)
    return truth


def _pick_operator(rng, cert: str | None) -> str:
    certified = {"WELD-2": ["OP-01", "OP-02"], "CNC-1": ["OP-01", "OP-03"],
                 "INSP-1": ["OP-03", "OP-04"]}
    if cert is None:
        return str(rng.choice(["OP-01", "OP-02", "OP-03", "OP-04", "OP-05"]))
    return str(rng.choice(certified[cert]))


def _attempt_violations(conn, all_units: dict[str, list[str]]) -> tuple[dict, dict]:
    """Try every illegal thing. Each must raise. Counts are the enforcement score."""
    attempted: dict[str, int] = {}
    blocked: dict[str, int] = {}

    def attempt(kind, fn):
        attempted[kind] = attempted.get(kind, 0) + 1
        try:
            fn()
        except ex.ExecutionError:
            blocked[kind] = blocked.get(kind, 0) + 1
        else:
            blocked.setdefault(kind, 0)

    conn.execute("INSERT INTO work_order VALUES ('WO-9999','BRKT-100',3,'RELEASED',?)",
                 (_ts(500).isoformat(),))
    for i in range(3):
        conn.execute(
            "INSERT INTO unit (unit_id, wo_id, serial, lot_qty, status, current_seq) "
            "VALUES (?,?,?,NULL,'IN_PROCESS',NULL)",
            (f"WO-9999-U{i+1:03d}", "WO-9999", f"SN9999{i+1:03d}"))
    conn.commit()
    v1, v2, v3 = [f"WO-9999-U{i+1:03d}" for i in range(3)]

    # 1. precedence: start op 30 with op 10 and 20 never done
    attempt("precedence", lambda: ex.start_operation(conn, v1, 30, "OP-01", "WC-MACH",
                                                     ts=_ts(501)))
    # 2. certification: uncertified OP-05 at a WELD-2 operation
    ex.start_operation(conn, v1, 10, "OP-02", "WC-CUT", ts=_ts(502))
    ex.issue_material(conn, v1, 10, "L-5100", 1.0, "OP-02", ts=_ts(502))
    ex.complete_operation(conn, v1, 10, "OP-02", "WC-CUT", ts=_ts(503))
    attempt("certification", lambda: ex.start_operation(conn, v1, 20, "OP-05", "WC-WELD",
                                                        ts=_ts(504)))
    # 3. over-issue: 3x the BOM quantity of steel bar
    ex.start_operation(conn, v2, 10, "OP-01", "WC-CUT", ts=_ts(505))
    attempt("over_issue", lambda: ex.issue_material(conn, v2, 10, "L-5100", 3.0, "OP-01",
                                                    ts=_ts(506)))
    # 4. off-BOM component: issuing powder at the cutting operation
    attempt("off_bom_component",
            lambda: ex.issue_material(conn, v2, 10, "L-7003", 0.15, "OP-01", ts=_ts(507)))
    # 5. double completion of the same operation without a rework entry
    ex.issue_material(conn, v2, 10, "L-5100", 1.0, "OP-01", ts=_ts(508))
    ex.complete_operation(conn, v2, 10, "OP-01", "WC-CUT", ts=_ts(509))
    attempt("double_completion",
            lambda: ex.complete_operation(conn, v2, 10, "OP-01", "WC-CUT", ts=_ts(510)))
    # 6. issue more than the lot has on hand
    attempt("insufficient_lot",
            lambda: ex.issue_material(conn, v3, 10, "L-4471-A", 99999, "OP-01",
                                      ts=_ts(511)))
    conn.commit()

    # 7. The AUTHORISED override must be allowed and must leave a trail. This is
    #    not a violation -- it is the exception path, and a system that cannot do
    #    it gets bypassed on paper instead.
    attempted["authorised_deviation_allowed"] = 1
    try:
        ex.start_operation(conn, v3, 30, "OP-05", "WC-MACH",
                           deviation_ref="DEV-2026-0148", ts=_ts(512))
        blocked["authorised_deviation_allowed"] = 0  # 0 = correctly NOT blocked
    except ex.ExecutionError:
        blocked["authorised_deviation_allowed"] = 1
    conn.commit()
    return attempted, blocked
