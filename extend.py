"""SE-2, the next 30%: the lot-tracked model exercised, property-based rework
tests, and a dispatch list.

    python extend.py
    python extend.py --report-only

Gaps the first build named:
  1. the lot-tracked product was DEFINED and never run, so the dual serial/lot
     model was designed and not demonstrated
  2. the rework state machine was exercised by 4 deterministic events, which is
     coverage rather than proof -- the spec explicitly asks for property testing
  3. no dispatch list, no WIP by work centre, no scheduling surface at all
"""
from __future__ import annotations

import itertools
import json
import pathlib
import sys
import time

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import execution as ex  # noqa: E402
import generate  # noqa: E402
import model  # noqa: E402
import trace  # noqa: E402

OUT = ROOT / "out"
DB = OUT / "mes_ext.db"


# ---------------------------------------------------------------------------
# 1. the lot-tracked product, actually run
# ---------------------------------------------------------------------------

def lot_tracked_stage(conn) -> dict:
    """Run PLATE-200, which is lot-tracked rather than serialised.

    THE DUAL MODEL, and why it is a real design problem rather than a config flag:

      SERIAL   one row per physical unit. Genealogy is per unit, and the recall
               question resolves to a specific object with a specific history.
      LOT      one row per BATCH, carrying a quantity. Genealogy is per batch,
               and a recall resolves to "some quantity within this batch" --
               which is a fundamentally weaker answer, and the weakness is
               physical, not a modelling shortcut. If 400 plates are sheared from
               one coil and mixed in a tote, no data model can tell you which
               plate came from which end of the coil.

    The consequence worth stating: **lot tracking makes exposure granular to the
    batch, so a recall on a lot-tracked product is always broader than on a
    serialised one.** That is a manufacturing decision (serialise or not) with a
    traceability cost, and it is the trade that gets made at product design and
    regretted during a recall.
    """
    ex.audit(conn, None, None, "WO_RELEASE", "WO-2001", "lot-tracked batch")
    conn.execute("INSERT INTO work_order VALUES ('WO-2001','PLATE-200',400,"
                 "'IN_PROCESS','2026-07-05T06:00:00+00:00')")
    # ONE unit row carrying the batch quantity -- that is the lot model.
    conn.execute("INSERT INTO unit (unit_id, wo_id, serial, lot_qty, status, "
                 "current_seq) VALUES ('WO-2001-B01','WO-2001',NULL,400,"
                 "'IN_PROCESS',NULL)")
    conn.commit()

    uid = "WO-2001-B01"
    ts = generate._ts
    ex.start_operation(conn, uid, 10, "OP-02", "WC-CUT", ts=ts(600))
    ex.issue_material(conn, uid, 10, "L-8001", 400.0, "OP-02", ts=ts(600))
    ex.complete_operation(conn, uid, 10, "OP-02", "WC-CUT", ts=ts(640))
    ex.start_operation(conn, uid, 20, "OP-03", "WC-MACH", ts=ts(650))
    # Partial scrap WITHIN the batch: 25 of 400 plates fail at drilling.
    ex.scrap(conn, uid, 20, "OP-03", "WC-MACH", "drill breakout", qty=25.0, ts=ts(700))
    ex.complete_operation(conn, uid, 20, "OP-03", "WC-MACH", qty=375.0, ts=ts(710))
    ex.start_operation(conn, uid, 30, "OP-04", "WC-INSP", ts=ts(720))
    ex.complete_operation(conn, uid, 30, "OP-04", "WC-INSP", qty=375.0, ts=ts(760))
    conn.commit()

    cons = ex.conservation_report(conn, "WO-2001")
    fwd = trace.forward(conn, "L-8001")
    return {
        "work_order": "WO-2001", "batch_qty": 400,
        "scrapped_within_batch": 25,
        "conservation": cons,
        "violations": sum(1 for c in cons if c["violates"]),
        "recall_units_from_L8001": len(fwd["unit_ids"]),
        "recall_granularity": "batch of 400 (one unit row)",
    }


# ---------------------------------------------------------------------------
# 2. property-based rework testing
# ---------------------------------------------------------------------------

def rework_properties(conn, n_cases: int = 200, seed: int = 5) -> dict:
    """Random routing histories, checking invariants that must hold for ALL of them.

    The spec asks for the rework state machine to be property-tested, and the
    first build exercised it with four deterministic events -- coverage, not
    proof. A property test asserts something true of EVERY history, then generates
    histories adversarially:

      P1  quantity conservation holds at every operation, always
      P2  precedence is never violated -- an operation never completes for a pass
          whose predecessor has not completed
      P3  a unit's completion count at an operation never exceeds its start count
      P4  every rework entry is preceded by an NCR with a REWORK disposition

    P3 is the one that catches the subtle rework bug: without the "most recent
    rework entry" comparison in complete_operation, a reworked unit can complete
    an operation twice for one pass and the ledger still balances at the work-order
    level while being wrong per unit.
    """
    rng = np.random.default_rng(seed)
    ts = generate._ts
    failures: list[dict] = []
    checked = 0
    t = 5000.0

    conn.execute("INSERT INTO work_order VALUES ('WO-3001','BRKT-100',400,"
                 "'IN_PROCESS','2026-07-06T06:00:00+00:00')")
    conn.commit()

    for case in range(n_cases):
        uid = f"WO-3001-U{case:04d}"
        conn.execute("INSERT INTO unit (unit_id, wo_id, serial, lot_qty, status, "
                     "current_seq) VALUES (?,?,?,NULL,'IN_PROCESS',NULL)",
                     (uid, "WO-3001", f"SN3001{case:04d}"))
        conn.commit()

        # Operators must hold the operation's certification -- the enforcement
        # rule is real and it refused the first version of this generator, which
        # is the correct outcome and a nuisance for a property test. The property
        # under test is the rework state machine, so the histories are generated
        # with valid operators rather than by disabling the gate.
        CERTIFIED = {10: ("OP-01", "OP-02", "OP-04"), 20: ("OP-01", "OP-02"),
                     30: ("OP-01", "OP-03"), 40: ("OP-01", "OP-04"),
                     50: ("OP-03", "OP-04")}
        seqs = [10, 20, 30, 40, 50]
        n_rework = int(rng.integers(0, 4))
        rework_points = sorted(rng.choice(seqs[1:], size=min(n_rework, 4),
                                          replace=False).tolist()) if n_rework else []
        done = set()
        try:
            for seq in seqs:
                t += 1
                op = str(rng.choice(CERTIFIED[seq]))
                wc = {10: "WC-CUT", 20: "WC-WELD", 30: "WC-MACH",
                      40: "WC-PAINT", 50: "WC-INSP"}[seq]
                ex.start_operation(conn, uid, seq, op, wc, ts=ts(t))
                if seq == 10:
                    ex.issue_material(conn, uid, 10, "L-5100", 1.0, op, ts=ts(t))
                elif seq == 20:
                    ex.issue_material(conn, uid, 20, "L-7001", 0.4, op, ts=ts(t))
                    ex.issue_material(conn, uid, 20, "L-7002", 2.0, op, ts=ts(t))
                elif seq == 40:
                    ex.issue_material(conn, uid, 40, "L-7003", 0.15, op, ts=ts(t))
                ex.complete_operation(conn, uid, seq, op, wc, ts=ts(t))
                done.add(seq)

                if seq in rework_points:
                    back_to = int(rng.choice([s for s in seqs if s < seq] or [10]))
                    ncr = ex.raise_ncr(conn, uid, seq, "property-test defect", ts=ts(t))
                    ex.disposition_ncr(conn, ncr, "REWORK", "OP-03",
                                       rework_to_seq=back_to, ts=ts(t))
                    for s2 in [s for s in seqs if s >= back_to and s <= seq]:
                        t += 1
                        op2 = str(rng.choice(CERTIFIED[s2]))
                        wc2 = {10: "WC-CUT", 20: "WC-WELD", 30: "WC-MACH",
                               40: "WC-PAINT", 50: "WC-INSP"}[s2]
                        ex.start_operation(conn, uid, s2, op2, wc2, ts=ts(t))
                        if s2 == 40:
                            ex.issue_material(conn, uid, 40, "L-7003", 0.15, op2,
                                              ts=ts(t))
                        elif s2 == 20:
                            ex.issue_material(conn, uid, 20, "L-7001", 0.4, op2, ts=ts(t))
                            ex.issue_material(conn, uid, 20, "L-7002", 2.0, op2, ts=ts(t))
                        elif s2 == 10:
                            ex.issue_material(conn, uid, 10, "L-5100", 1.0, op2, ts=ts(t))
                        ex.complete_operation(conn, uid, s2, op2, wc2, ts=ts(t))
            conn.commit()
        except ex.ExecutionError as e:
            failures.append({"case": case, "unit": uid, "stage": "build",
                             "error": str(e)[:160]})
            conn.commit()
            continue

        checked += 1
        for name, ok, detail in _check_properties(conn, uid):
            if not ok:
                failures.append({"case": case, "unit": uid, "property": name,
                                 "detail": detail})

    cons = ex.conservation_report(conn, "WO-3001")
    return {
        "cases_generated": n_cases, "cases_completed": checked,
        "property_failures": failures[:10],
        "n_property_failures": len(failures),
        "conservation_violations": sum(1 for c in cons if c["violates"]),
        "operations_checked": len(cons),
        "total_rework_entries": int(conn.execute(
            "SELECT COUNT(*) FROM op_record WHERE wo_id='WO-3001' "
            "AND action='REWORK_ENTRY'").fetchone()[0]),
    }


def _check_properties(conn, unit_id: str):
    rows = conn.execute(
        "SELECT seq, action, rec_id FROM op_record WHERE unit_id=? ORDER BY rec_id",
        (unit_id,)).fetchall()

    starts: dict[int, int] = {}
    completes: dict[int, int] = {}
    reworks: dict[int, int] = {}
    for seq, action, _ in rows:
        if action == "START":
            starts[seq] = starts.get(seq, 0) + 1
        elif action == "COMPLETE":
            completes[seq] = completes.get(seq, 0) + 1
        elif action == "REWORK_ENTRY":
            reworks[seq] = reworks.get(seq, 0) + 1

    # P3: completions never exceed starts at any operation.
    for seq, n in completes.items():
        if n > starts.get(seq, 0):
            yield ("P3 completions <= starts", False,
                   f"op {seq}: {n} completes vs {starts.get(seq, 0)} starts")

    # P4: every rework entry has a REWORK-dispositioned NCR.
    n_rework = sum(reworks.values())
    n_ncr = conn.execute(
        "SELECT COUNT(*) FROM ncr WHERE unit_id=? AND disposition='REWORK'",
        (unit_id,)).fetchone()[0]
    if n_rework != n_ncr:
        yield ("P4 rework entries match REWORK NCRs", False,
               f"{n_rework} rework entries vs {n_ncr} REWORK NCRs")

    # P2: the last completion of each op is preceded by a completion of its
    # predecessor somewhere earlier in the ledger.
    order = [10, 20, 30, 40, 50]
    seen_complete: set[int] = set()
    for seq, action, _ in rows:
        if action == "COMPLETE":
            i = order.index(seq) if seq in order else 0
            if i > 0 and order[i - 1] not in seen_complete:
                yield ("P2 precedence", False,
                       f"op {seq} completed before op {order[i-1]}")
            seen_complete.add(seq)
    yield ("ok", True, "")


# ---------------------------------------------------------------------------
# 3. dispatch list
# ---------------------------------------------------------------------------

def _release_wip(conn, n: int = 18) -> None:
    """Release units and advance them varying distances along the routing."""
    ts = generate._ts
    conn.execute("INSERT INTO work_order VALUES ('WO-4001','BRKT-100',?, "
                 "'IN_PROCESS','2026-07-08T06:00:00+00:00')", (n,))
    conn.commit()
    CERT = {10: "OP-01", 20: "OP-02", 30: "OP-03", 40: "OP-01", 50: "OP-03"}
    WC = {10: "WC-CUT", 20: "WC-WELD", 30: "WC-MACH", 40: "WC-PAINT",
          50: "WC-INSP"}
    t = 9000.0
    for i in range(n):
        uid = f"WO-4001-U{i:03d}"
        conn.execute("INSERT INTO unit (unit_id, wo_id, serial, lot_qty, status, "
                     "current_seq) VALUES (?,?,?,NULL,'IN_PROCESS',NULL)",
                     (uid, "WO-4001", f"SN4001{i:03d}"))
        # Stop after a varying number of operations, so the queues differ.
        upto = [10, 20, 30, 40][i % 4]
        for seq in (10, 20, 30, 40, 50):
            if seq > upto:
                break
            t += 1
            op, wc = CERT[seq], WC[seq]
            ex.start_operation(conn, uid, seq, op, wc, ts=ts(t))
            if seq == 10:
                ex.issue_material(conn, uid, 10, "L-5100", 1.0, op, ts=ts(t))
            elif seq == 20:
                ex.issue_material(conn, uid, 20, "L-7001", 0.4, op, ts=ts(t))
                ex.issue_material(conn, uid, 20, "L-7002", 2.0, op, ts=ts(t))
            elif seq == 40:
                ex.issue_material(conn, uid, 40, "L-7003", 0.15, op, ts=ts(t))
            ex.complete_operation(conn, uid, seq, op, wc, ts=ts(t))
    conn.commit()


def dispatch_stage(conn) -> dict:
    """What each work centre should run next, and how much WIP is queued there.

    The MES side of scheduling: not planning (that is ERP, level 4), but the
    dispatch list -- given what is in process right now, what is the next
    operation at each work centre and what is waiting. It is derived entirely
    from the execution ledger, which is the point: no separate scheduling state
    to drift out of sync with reality.
    """
    rows = conn.execute("""
        SELECT u.unit_id, u.wo_id, u.status, u.current_seq, w.sku
        FROM unit u JOIN work_order w ON w.wo_id = u.wo_id
        WHERE u.status = 'IN_PROCESS'
    """).fetchall()

    queues: dict[str, list] = {}
    for r in rows:
        nxt = conn.execute(
            "SELECT seq, wc_id, name, std_run_s FROM operation "
            "WHERE sku=? AND seq > COALESCE(?, 0) ORDER BY seq LIMIT 1",
            (r["sku"], r["current_seq"])).fetchone()
        if nxt is None:
            continue
        queues.setdefault(nxt["wc_id"], []).append({
            "unit": r["unit_id"], "wo": r["wo_id"], "next_op": nxt["seq"],
            "op_name": nxt["name"], "std_run_s": nxt["std_run_s"],
        })

    out = []
    for wc, items in sorted(queues.items()):
        cap = conn.execute("SELECT capacity FROM work_center WHERE wc_id=?",
                           (wc,)).fetchone()
        load_s = sum(i["std_run_s"] for i in items)
        out.append({
            "work_center": wc,
            "capacity": cap["capacity"] if cap else 1,
            "units_queued": len(items),
            "queued_work_hours": load_s / 3600,
            "hours_per_resource": load_s / 3600 / max(1, cap["capacity"] if cap else 1),
            "next_units": [i["unit"] for i in items[:4]],
        })
    return {"queues": sorted(out, key=lambda r: -r["hours_per_resource"])}


# ---------------------------------------------------------------------------

def main() -> None:
    OUT.mkdir(exist_ok=True)
    if "--report-only" in sys.argv:
        prev = json.loads((OUT / "extensions.json").read_text())
        (ROOT / "docs" / "EXTENSIONS.md").write_text(report(prev), encoding="utf-8")
        print("re-rendered docs/EXTENSIONS.md")
        return

    t0 = time.perf_counter()
    for suffix in ("", "-wal", "-shm"):
        pathlib.Path(str(DB) + suffix).unlink(missing_ok=True)
    conn = model.create(DB)
    generate.seed_definitions(conn)
    generate.seed_lots(conn)

    res: dict = {}
    print("1/3 the lot-tracked product, actually run ...", flush=True)
    res["lot_tracked"] = lot_tracked_stage(conn)
    print(f"    batch of {res['lot_tracked']['batch_qty']}, "
          f"{res['lot_tracked']['scrapped_within_batch']} scrapped within it, "
          f"{res['lot_tracked']['violations']} conservation violations", flush=True)

    quick = "--quick" in sys.argv
    print("2/3 property-based rework testing ...", flush=True)
    res["properties"] = rework_properties(conn, n_cases=40 if quick else 200)
    p = res["properties"]
    print(f"    {p['cases_completed']}/{p['cases_generated']} random histories, "
          f"{p['total_rework_entries']} rework entries, "
          f"{p['n_property_failures']} property failures, "
          f"{p['conservation_violations']} conservation violations", flush=True)

    print("3/3 dispatch list ...", flush=True)
    # A dispatch list needs work IN PROCESS. The property-test units all run to
    # completion, so release a batch that is deliberately mid-route -- which is
    # also the realistic case: a dispatch list on a plant with no WIP is a plant
    # that is not running.
    _release_wip(conn)
    res["dispatch"] = dispatch_stage(conn)
    res["wall_seconds"] = time.perf_counter() - t0

    (OUT / "extensions.json").write_text(json.dumps(res, indent=2, default=str))
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "EXTENSIONS.md").write_text(report(res), encoding="utf-8")
    print(f"\nwrote docs/EXTENSIONS.md ({res['wall_seconds']:.0f}s)")


def report(res: dict) -> str:
    L: list[str] = []
    A = L.append
    A("# SE-2 extensions — generated by `extend.py`, not hand-edited\n")

    lt = res["lot_tracked"]
    A("## 1. The lot-tracked model, actually exercised\n")
    A("The first build *defined* a lot-tracked product and never ran one, so the "
      "dual serial/lot model was designed and not demonstrated. `PLATE-200` now "
      f"runs as a batch of {lt['batch_qty']} with "
      f"{lt['scrapped_within_batch']} plates scrapped **within** the batch at "
      "drilling.\n")
    A("| work order | op | started | completed | scrapped | in process |")
    A("|---|---|---|---|---|---|")
    for c in lt["conservation"]:
        A(f"| {c['wo_id']} | {c['seq']} | {c['started']:.0f} | "
          f"{c['completed']:.0f} | {c['scrapped']:.0f} | {c['in_process']:.0f} |")
    A(f"\n{lt['violations']} conservation violations. **Quantity conservation "
      "works on quantities, not on unit counts** — which is what makes one ledger "
      "serve both models: a serialised unit is simply a batch of one.\n")
    A("**The traceability cost, which is the part worth understanding.** A "
      "serialised product resolves a recall to a specific object with a specific "
      "history. A lot-tracked product resolves it to *some quantity within a "
      f"batch* — here, {lt['recall_granularity']}. That is not a modelling "
      "shortcut, it is physical: if 400 plates are sheared from one coil and mixed "
      "in a tote, no data model can say which plate came from which end. "
      "**Serialise or not is a product-design decision with a traceability price**, "
      "and it is the trade that gets made at design and regretted during a recall.")

    p = res["properties"]
    A("\n## 2. Property-based testing of the rework state machine\n")
    A("The spec asks for this explicitly, and the first build offered four "
      "deterministic rework events — coverage, not proof. Here "
      f"**{p['cases_generated']} randomly generated routing histories** with 0–3 "
      "rework loops each, re-entering at randomly chosen earlier operations, are "
      "checked against invariants that must hold for *every* history:\n")
    A("| property | holds |")
    A("|---|---|")
    A("| P1 — quantity conservation at every operation | "
      f"**{p['conservation_violations']} violations** across "
      f"{p['operations_checked']} (order, op) pairs |")
    A("| P2 — precedence never violated | see failures below |")
    A("| P3 — completions never exceed starts at any operation | see failures below |")
    A("| P4 — every rework entry has a REWORK-dispositioned NCR | see failures below |")
    A(f"\n**{p['cases_completed']}/{p['cases_generated']} histories built "
      f"successfully, generating {p['total_rework_entries']} rework entries. "
      f"Property failures: {p['n_property_failures']}.**\n")
    if p["n_property_failures"]:
        A("| case | property | detail |")
        A("|---|---|---|")
        for f in p["property_failures"]:
            A(f"| {f.get('case')} | {f.get('property', f.get('stage'))} | "
              f"{f.get('detail', f.get('error'))} |")
    A("\n**P3 is the one that earns its place.** Without the \"most recent rework "
      "entry\" comparison in `complete_operation`, a reworked unit can complete an "
      "operation twice for a single pass — and the work-order ledger still "
      "balances, because the extra completion is offset elsewhere. The violation "
      "is only visible per unit per operation, which is exactly the granularity a "
      "hand-written test tends not to check and a property test does by "
      "construction.")

    d = res["dispatch"]
    A("\n## 3. The dispatch list\n")
    A("Not planning — that is ERP at ISA-95 level 4. This is the **dispatch list**: "
      "given what is in process right now, what runs next at each work centre and "
      "how much work is queued there.\n")
    A("| work centre | capacity | units queued | queued work (h) | hours per resource | next up |")
    A("|---|---|---|---|---|---|")
    for q in d["queues"]:
        A(f"| {q['work_center']} | {q['capacity']} | {q['units_queued']} | "
          f"{q['queued_work_hours']:.2f} | **{q['hours_per_resource']:.2f}** | "
          f"{', '.join(q['next_units'][:3])} |")
    if d["queues"]:
        top = d["queues"][0]
        A(f"\n**{top['work_center']} carries the most load per resource** "
          f"({top['hours_per_resource']:.2f} h). Sorting by *hours per resource* "
          "rather than by unit count is the whole point — a work centre with three "
          "machines and twelve units queued is in better shape than one with a "
          "single machine and five.\n")
    A("**It is derived entirely from the execution ledger**, with no separate "
      "scheduling state. That is deliberate: a dispatch list held in its own table "
      "drifts out of sync with what actually happened on the floor, and then the "
      "operators stop trusting it and go back to the whiteboard. Deriving it means "
      "it cannot disagree with the record.\n")
    A("**Still not built:** finite-capacity scheduling, due-date sequencing, setup "
      "matrices, or any notion of when a work centre becomes free. This is a "
      "priority list, not a schedule.")

    A("\n---\n*Regenerate with `python extend.py`.*")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
