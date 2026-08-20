"""Traceability: forward, backward, and the recall drill.

This is the centre of the domain. Work-order CRUD is everywhere; forward-and-
backward genealogy done correctly is the part recalls, audits and customer-quality
escapes all depend on.

FORWARD  lot -> every unit that consumed it -> orders, operations, shipments,
         customers. The question a supplier notification asks.
BACKWARD unit -> its complete build record: operations, operators, machines,
         material lots, NCRs. The "birth certificate". The question a returned
         part asks.
"""
from __future__ import annotations

import datetime as dt
import time


def affected_lots(conn, lot_id: str) -> set[str]:
    """The recalled lot plus every lot split from it, transitively.

    Both directions matter and only one is obvious. DOWN: a split child inherits
    the defect, so children are in scope. UP: if the recalled id is itself a split
    child, its parent's OTHER children came from the same original material -- so
    the parent is walked up and back down.

    Stopping at the named lot is the failure mode that makes a recall look complete
    and leaves product in the field.
    """
    seen: set[str] = set()
    # walk up to the root
    root = lot_id
    while True:
        r = conn.execute("SELECT parent_lot FROM lot WHERE lot_id=?", (root,)).fetchone()
        if r is None or r["parent_lot"] is None:
            break
        root = r["parent_lot"]
    # walk down from the root
    frontier = [root]
    while frontier:
        cur = frontier.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for c in conn.execute("SELECT lot_id FROM lot WHERE parent_lot=?", (cur,)):
            frontier.append(c["lot_id"])
    return seen


def forward(conn, lot_id: str) -> dict:
    """lot -> units -> work orders -> shipments -> customers."""
    lots = affected_lots(conn, lot_id)
    qmarks = ",".join("?" * len(lots))
    units = conn.execute(
        f"SELECT DISTINCT c.unit_id, c.wo_id, c.seq, c.lot_id, c.qty, u.status, u.serial "
        f"FROM consumption c JOIN unit u ON u.unit_id=c.unit_id "
        f"WHERE c.lot_id IN ({qmarks}) ORDER BY c.unit_id", tuple(lots)).fetchall()
    unit_ids = sorted({u["unit_id"] for u in units})
    ship = []
    if unit_ids:
        q2 = ",".join("?" * len(unit_ids))
        ship = conn.execute(
            f"SELECT s.ship_id, s.customer, s.shipped_at, l.unit_id "
            f"FROM shipment_line l JOIN shipment s ON s.ship_id=l.ship_id "
            f"WHERE l.unit_id IN ({q2})", tuple(unit_ids)).fetchall()
    by_status: dict[str, int] = {}
    for u in units:
        by_status[u["status"]] = by_status.get(u["status"], 0) + 1
    return {
        "recalled_lot": lot_id,
        "lots_in_scope": sorted(lots),
        "units": [dict(u) for u in units],
        "unit_ids": unit_ids,
        "units_by_status": by_status,
        "shipments": [dict(s) for s in ship],
        "customers": sorted({s["customer"] for s in ship}),
    }


def backward(conn, unit_id: str) -> dict:
    """The birth certificate: everything that happened to one unit."""
    unit = conn.execute(
        "SELECT u.*, w.sku, w.qty AS wo_qty FROM unit u "
        "JOIN work_order w ON w.wo_id=u.wo_id WHERE u.unit_id=?", (unit_id,)).fetchone()
    if unit is None:
        return {}
    ops = conn.execute(
        "SELECT seq, action, qty, op_id, wc_id, reason, deviation_ref, ts "
        "FROM op_record WHERE unit_id=? ORDER BY rec_id", (unit_id,)).fetchall()
    mats = conn.execute(
        "SELECT c.seq, c.component, c.lot_id, c.qty, c.op_id, l.supplier, l.parent_lot "
        "FROM consumption c JOIN lot l ON l.lot_id=c.lot_id "
        "WHERE c.unit_id=? ORDER BY c.seq", (unit_id,)).fetchall()
    ncrs = conn.execute(
        "SELECT * FROM ncr WHERE unit_id=? ORDER BY ncr_id", (unit_id,)).fetchall()
    shp = conn.execute(
        "SELECT s.* FROM shipment_line l JOIN shipment s ON s.ship_id=l.ship_id "
        "WHERE l.unit_id=?", (unit_id,)).fetchone()
    return {
        "unit": dict(unit),
        "operations": [dict(o) for o in ops],
        "materials": [dict(m) for m in mats],
        "ncrs": [dict(n) for n in ncrs],
        "shipment": dict(shp) if shp else None,
        "operators_involved": sorted({o["op_id"] for o in ops if o["op_id"]}),
        "passes_per_operation": _passes(ops),
    }


def _passes(ops) -> dict[int, int]:
    """How many times did this unit go through each operation?

    The number a quality engineer asks for first when investigating a systemic
    defect, and the number that is unrecoverable if rework is modelled as a status
    flag rather than as a routing event.
    """
    out: dict[int, int] = {}
    for o in ops:
        if o["action"] in ("START", "REWORK_ENTRY"):
            out[o["seq"]] = out.get(o["seq"], 0) + (1 if o["action"] == "START" else 0)
    for o in ops:
        if o["action"] == "REWORK_ENTRY":
            out[o["seq"]] = out.get(o["seq"], 0) + 1
    return out


def birth_certificate(conn, unit_id: str) -> str:
    """Render the build record as the document a customer or auditor receives."""
    b = backward(conn, unit_id)
    if not b:
        return f"unit {unit_id} not found"
    u = b["unit"]
    L = [f"BUILD RECORD — unit {unit_id}",
         f"  product      : {u['sku']}",
         f"  work order   : {u['wo_id']} (order qty {u['wo_qty']})",
         f"  serial / lot : {u['serial'] or '-'} / {u['lot_qty'] or '-'}",
         f"  status       : {u['status']}", "",
         "  OPERATIONS"]
    for o in b["operations"]:
        dev = f"  [DEVIATION {o['deviation_ref']}]" if o["deviation_ref"] else ""
        rsn = f"  ({o['reason']})" if o["reason"] else ""
        L.append(f"    op {o['seq']:>3}  {o['action']:<13} by {o['op_id'] or '-':<6} "
                 f"at {o['wc_id'] or '-':<8} {o['ts'][:19]}{rsn}{dev}")
    L += ["", "  MATERIAL CONSUMED"]
    for m in b["materials"]:
        par = f" (split from {m['parent_lot']})" if m["parent_lot"] else ""
        L.append(f"    op {m['seq']:>3}  {m['component']:<12} lot {m['lot_id']:<12} "
                 f"qty {m['qty']:<7} supplier {m['supplier']}{par}")
    if b["ncrs"]:
        L += ["", "  NONCONFORMANCES"]
        for n in b["ncrs"]:
            L.append(f"    NCR-{n['ncr_id']} op {n['seq']} {n['defect']} -> "
                     f"{n['disposition']} (approved {n['approved_by']})")
    if b["shipment"]:
        s = b["shipment"]
        L += ["", f"  SHIPPED  {s['ship_id']} to {s['customer']} on {s['shipped_at'][:10]}"]
    return "\n".join(L)


def recall_drill(conn, lot_id: str) -> dict:
    """One command: supplier flags a lot -> full exposure + quarantine actions.

    Timed, because "we can work that out" and "here is the answer in 40
    milliseconds" are different capabilities when a customer is on the phone.
    """
    t0 = time.perf_counter()
    f = forward(conn, lot_id)
    shipped, in_process, complete, scrapped = [], [], [], []
    shipped_units = {s["unit_id"] for s in f["shipments"]}
    for u in f["units"]:
        uid = u["unit_id"]
        if uid in shipped_units:
            shipped.append(uid)
        elif u["status"] == "IN_PROCESS":
            in_process.append(uid)
        elif u["status"] == "SCRAPPED":
            scrapped.append(uid)
        else:
            complete.append(uid)

    actions = []
    for uid in sorted(set(in_process)):
        actions.append({"action": "QUARANTINE_WIP", "unit_id": uid})
    for uid in sorted(set(complete)):
        actions.append({"action": "HOLD_FINISHED_GOODS", "unit_id": uid})
    for s in f["shipments"]:
        actions.append({"action": "CUSTOMER_NOTIFICATION",
                        "customer": s["customer"], "shipment": s["ship_id"],
                        "unit_id": s["unit_id"]})
    elapsed = time.perf_counter() - t0
    return {
        "recalled_lot": lot_id,
        "lots_in_scope": f["lots_in_scope"],
        "units_affected": len(set(f["unit_ids"])),
        "shipped": sorted(set(shipped)),
        "finished_on_hand": sorted(set(complete)),
        "in_process": sorted(set(in_process)),
        "already_scrapped": sorted(set(scrapped)),
        "customers": f["customers"],
        "actions": actions,
        "query_seconds": elapsed,
    }


def quarantine(conn, unit_ids: list[str], ts=None) -> int:
    n = 0
    for uid in unit_ids:
        cur = conn.execute(
            "UPDATE unit SET status='QUARANTINED' WHERE unit_id=? AND status='IN_PROCESS'",
            (uid,))
        n += cur.rowcount
    return n
