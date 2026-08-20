"""Execution rules. Where correctness lives.

Every function here either records a transaction or REFUSES to, and the refusals
are the product. A system that records whatever it is told is a log file; a system
that enforces precedence, certification, and quantity conservation is an execution
system.

The refusals, and why each one exists:

  PrecedenceError     op 20 cannot complete before op 10. Without it, a routing is
                      decoration and the build record is a set of unordered claims.
  CertificationError  an uncertified operator cannot record work. The override path
                      is a LOGGED APPROVAL, never the absence of the check --
                      rigid refusal and silent bypass both fail in a real plant, so
                      the third option has to exist and has to leave a trail.
  ConservationError   started == completed + scrapped + in_process at every
                      operation. Manufacturing's double-entry bookkeeping.
  IssueError          over-issue beyond tolerance, or issuing from a lot with
                      insufficient on-hand quantity.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

OVER_ISSUE_TOLERANCE = 1.10  # 10% over the BOM quantity is allowed without approval


class ExecutionError(Exception):
    """Base for every refusal. Each carries the reason a human needs."""


class PrecedenceError(ExecutionError):
    pass


class CertificationError(ExecutionError):
    pass


class ConservationError(ExecutionError):
    pass


class IssueError(ExecutionError):
    pass


def _now(clock=None) -> str:
    return (clock or dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)).isoformat()


def audit(conn, op_id, station, action, entity, detail, ts=None) -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, op_id, station, action, entity, detail) "
        "VALUES (?,?,?,?,?,?)",
        (ts or _now(), op_id, station, action, entity, detail),
    )


def routing(conn, sku: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM operation WHERE sku=? ORDER BY seq", (sku,)).fetchall()


def prior_seq(conn, sku: str, seq: int) -> int | None:
    r = conn.execute(
        "SELECT MAX(seq) AS s FROM operation WHERE sku=? AND seq<?", (sku, seq)).fetchone()
    return r["s"]


def _unit(conn, unit_id: str) -> sqlite3.Row:
    u = conn.execute("SELECT * FROM unit WHERE unit_id=?", (unit_id,)).fetchone()
    if u is None:
        raise ExecutionError(f"unknown unit {unit_id}")
    return u


def _sku_of(conn, unit_id: str) -> str:
    return conn.execute(
        "SELECT p.sku FROM unit u JOIN work_order w ON w.wo_id=u.wo_id "
        "JOIN product p ON p.sku=w.sku WHERE u.unit_id=?", (unit_id,)).fetchone()["sku"]


def check_certification(conn, op_id: str, sku: str, seq: int,
                        deviation_ref: str | None = None) -> None:
    row = conn.execute(
        "SELECT cert_required FROM operation WHERE sku=? AND seq=?", (sku, seq)).fetchone()
    need = row["cert_required"] if row else None
    if not need:
        return
    has = conn.execute(
        "SELECT 1 FROM certification WHERE op_id=? AND cert=?", (op_id, need)).fetchone()
    if has:
        return
    if deviation_ref:
        # The override path. It is not "skip the check"; it is "the check failed
        # and somebody with authority accepted that, and their name is on it".
        return
    raise CertificationError(
        f"operator {op_id} lacks certification {need!r} required at {sku} op {seq}; "
        "record an authorised deviation reference to override")


def check_precedence(conn, unit_id: str, seq: int) -> None:
    sku = _sku_of(conn, unit_id)
    p = prior_seq(conn, sku, seq)
    if p is None:
        return
    done = conn.execute(
        "SELECT 1 FROM op_record WHERE unit_id=? AND seq=? AND action='COMPLETE'",
        (unit_id, p)).fetchone()
    if not done:
        raise PrecedenceError(
            f"unit {unit_id} cannot start op {seq}: op {p} is not complete")


def start_operation(conn, unit_id: str, seq: int, op_id: str, wc_id: str,
                    deviation_ref: str | None = None, ts=None) -> None:
    u = _unit(conn, unit_id)
    if u["status"] in ("SCRAPPED", "QUARANTINED"):
        raise ExecutionError(f"unit {unit_id} is {u['status']}")
    sku = _sku_of(conn, unit_id)
    if deviation_ref is None:
        check_precedence(conn, unit_id, seq)
    check_certification(conn, op_id, sku, seq, deviation_ref)
    conn.execute(
        "INSERT INTO op_record (wo_id, unit_id, seq, action, qty, op_id, wc_id, "
        "deviation_ref, ts) VALUES (?,?,?,'START',?,?,?,?,?)",
        (u["wo_id"], unit_id, seq, u["lot_qty"] or 1, op_id, wc_id, deviation_ref, _now(ts)))
    conn.execute("UPDATE unit SET current_seq=? WHERE unit_id=?", (seq, unit_id))
    audit(conn, op_id, wc_id, "START", unit_id,
          f"seq={seq}" + (f" deviation={deviation_ref}" if deviation_ref else ""), _now(ts))


def issue_material(conn, unit_id: str, seq: int, lot_id: str, qty: float,
                   op_id: str, ts=None, allow_over_issue: bool = False) -> None:
    """Create the genealogy edge. This is the transaction traceability is made of."""
    u = _unit(conn, unit_id)
    lot = conn.execute("SELECT * FROM lot WHERE lot_id=?", (lot_id,)).fetchone()
    if lot is None:
        raise IssueError(f"unknown lot {lot_id}")
    if lot["qty_on_hand"] < qty - 1e-9:
        raise IssueError(
            f"lot {lot_id} has {lot['qty_on_hand']} on hand, cannot issue {qty}")

    sku = _sku_of(conn, unit_id)
    bom = conn.execute(
        "SELECT qty_per FROM bom_line WHERE sku=? AND seq=? AND component=?",
        (sku, seq, lot["component"])).fetchone()
    if bom is None:
        raise IssueError(
            f"component {lot['component']} is not on the BOM for {sku} op {seq} -- "
            "issuing it here would create a genealogy edge the routing does not "
            "explain, which is worse than no edge at all")
    # The over-issue budget is PER PASS, not per unit lifetime.
    #
    # This was a bug and it is worth keeping the explanation. A reworked unit goes
    # through the operation a second time and legitimately consumes the material a
    # second time -- a repainted bracket really does use a second dose of powder.
    # With a per-lifetime budget the second issue reads as a 200% over-issue and
    # the system refuses it, which makes rework impossible to record correctly.
    # The plant's response to that is to issue the material against some other
    # unit, and the genealogy silently becomes fiction.
    #
    # So the expected quantity scales with the number of passes: one for the
    # original, plus one per rework entry at this operation.
    passes = 1 + conn.execute(
        "SELECT COUNT(*) AS n FROM op_record WHERE unit_id=? AND seq=? "
        "AND action='REWORK_ENTRY'", (unit_id, seq)).fetchone()["n"]
    expected = bom["qty_per"] * (u["lot_qty"] or 1) * passes
    already = conn.execute(
        "SELECT COALESCE(SUM(qty),0) AS q FROM consumption WHERE unit_id=? AND seq=? "
        "AND component=?", (unit_id, seq, lot["component"])).fetchone()["q"]
    if (already + qty) > expected * OVER_ISSUE_TOLERANCE + 1e-9 and not allow_over_issue:
        raise IssueError(
            f"over-issue: {already + qty} against an expected {expected} "
            f"({passes} pass(es) x {bom['qty_per']} per unit, "
            f"+{(OVER_ISSUE_TOLERANCE-1)*100:.0f}% tolerance) for {lot['component']}")

    conn.execute(
        "INSERT INTO consumption (unit_id, wo_id, seq, lot_id, component, qty, op_id, ts) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (unit_id, u["wo_id"], seq, lot_id, lot["component"], qty, op_id, _now(ts)))
    conn.execute("UPDATE lot SET qty_on_hand = qty_on_hand - ? WHERE lot_id=?",
                 (qty, lot_id))
    audit(conn, op_id, None, "ISSUE", unit_id,
          f"lot={lot_id} qty={qty} seq={seq}", _now(ts))


def split_lot(conn, lot_id: str, new_lot_id: str, qty: float, ts=None) -> str:
    """Split a lot, preserving lineage through `parent_lot`.

    A split that loses the parent link is how a recall stops at the split: the
    child looks like a new lot with no supplier history. `trace.affected_lots()`
    walks the parent chain in both directions for exactly this reason.
    """
    lot = conn.execute("SELECT * FROM lot WHERE lot_id=?", (lot_id,)).fetchone()
    if lot is None:
        raise IssueError(f"unknown lot {lot_id}")
    if lot["qty_on_hand"] < qty - 1e-9:
        raise IssueError(f"cannot split {qty} from {lot_id}: {lot['qty_on_hand']} on hand")
    conn.execute(
        "INSERT INTO lot (lot_id, component, supplier, qty_received, qty_on_hand, "
        "parent_lot, received_at) VALUES (?,?,?,?,?,?,?)",
        (new_lot_id, lot["component"], lot["supplier"], qty, qty, lot_id, _now(ts)))
    conn.execute("UPDATE lot SET qty_on_hand = qty_on_hand - ? WHERE lot_id=?",
                 (qty, lot_id))
    audit(conn, None, None, "SPLIT", new_lot_id, f"from={lot_id} qty={qty}", _now(ts))
    return new_lot_id


def complete_operation(conn, unit_id: str, seq: int, op_id: str, wc_id: str,
                       qty: float | None = None, ts=None,
                       deviation_ref: str | None = None) -> None:
    u = _unit(conn, unit_id)
    started = conn.execute(
        "SELECT 1 FROM op_record WHERE unit_id=? AND seq=? AND action='START'",
        (unit_id, seq)).fetchone()
    if not started:
        raise PrecedenceError(f"unit {unit_id} op {seq} was never started")
    already = conn.execute(
        "SELECT 1 FROM op_record WHERE unit_id=? AND seq=? AND action='COMPLETE' "
        "AND rec_id > COALESCE((SELECT MAX(rec_id) FROM op_record WHERE unit_id=? "
        "AND seq=? AND action='REWORK_ENTRY'), 0)",
        (unit_id, seq, unit_id, seq)).fetchone()
    if already:
        raise ConservationError(
            f"unit {unit_id} op {seq} is already complete for this pass; a second "
            "completion without an intervening rework entry would double-count")
    conn.execute(
        "INSERT INTO op_record (wo_id, unit_id, seq, action, qty, op_id, wc_id, "
        "deviation_ref, ts) VALUES (?,?,?,'COMPLETE',?,?,?,?,?)",
        (u["wo_id"], unit_id, seq, qty if qty is not None else (u["lot_qty"] or 1),
         op_id, wc_id, deviation_ref, _now(ts)))
    audit(conn, op_id, wc_id, "COMPLETE", unit_id, f"seq={seq}", _now(ts))

    last = conn.execute("SELECT MAX(seq) AS s FROM operation WHERE sku=?",
                        (_sku_of(conn, unit_id),)).fetchone()["s"]
    if seq == last:
        conn.execute("UPDATE unit SET status='COMPLETE' WHERE unit_id=?", (unit_id,))


def scrap(conn, unit_id: str, seq: int, op_id: str, wc_id: str, reason: str,
          qty: float | None = None, ts=None) -> None:
    u = _unit(conn, unit_id)
    conn.execute(
        "INSERT INTO op_record (wo_id, unit_id, seq, action, qty, op_id, wc_id, "
        "reason, ts) VALUES (?,?,?,'SCRAP',?,?,?,?,?)",
        (u["wo_id"], unit_id, seq, qty if qty is not None else (u["lot_qty"] or 1),
         op_id, wc_id, reason, _now(ts)))
    conn.execute("UPDATE unit SET status='SCRAPPED' WHERE unit_id=?", (unit_id,))
    audit(conn, op_id, wc_id, "SCRAP", unit_id, f"seq={seq} reason={reason}", _now(ts))


def raise_ncr(conn, unit_id: str, seq: int, defect: str, ts=None) -> int:
    cur = conn.execute(
        "INSERT INTO ncr (unit_id, seq, defect, raised_at) VALUES (?,?,?,?)",
        (unit_id, seq, defect, _now(ts)))
    audit(conn, None, None, "NCR_RAISED", unit_id, f"seq={seq} defect={defect}", _now(ts))
    return int(cur.lastrowid)


def disposition_ncr(conn, ncr_id: int, disposition: str, approved_by: str,
                    rework_to_seq: int | None = None, ts=None) -> None:
    """Close an NCR. REWORK re-enters the routing at a defined operation.

    Rework re-entry is the genuinely tricky state problem in an MES, and it is
    tricky for a specific reason: the unit must be allowed to complete an operation
    it has ALREADY completed. Every naive precedence and
    already-completed check refuses that, correctly, for a first pass -- so the
    rework entry has to be a first-class event that resets the pass, rather than a
    status flag. `REWORK_ENTRY` in `op_record` is that event, and
    `complete_operation` compares against the most recent one.

    Handling rework as a status field instead is the common shortcut, and it loses
    the operation history: you can no longer answer "how many times did this unit
    go through op 30", which is exactly what a quality engineer investigating a
    systemic defect asks first.
    """
    n = conn.execute("SELECT * FROM ncr WHERE ncr_id=?", (ncr_id,)).fetchone()
    if n is None:
        raise ExecutionError(f"unknown NCR {ncr_id}")
    if disposition not in ("REWORK", "USE_AS_IS", "SCRAP"):
        raise ExecutionError(f"invalid disposition {disposition}")
    if disposition == "REWORK" and rework_to_seq is None:
        raise ExecutionError("REWORK requires the operation to re-enter at")

    conn.execute(
        "UPDATE ncr SET disposition=?, rework_to_seq=?, approved_by=?, closed_at=? "
        "WHERE ncr_id=?",
        (disposition, rework_to_seq, approved_by, _now(ts), ncr_id))
    unit_id = n["unit_id"]
    if disposition == "SCRAP":
        conn.execute("UPDATE unit SET status='SCRAPPED' WHERE unit_id=?", (unit_id,))
        u = _unit(conn, unit_id)
        conn.execute(
            "INSERT INTO op_record (wo_id, unit_id, seq, action, qty, op_id, reason, ts) "
            "VALUES (?,?,?,'SCRAP',?,?,?,?)",
            (u["wo_id"], unit_id, n["seq"], u["lot_qty"] or 1, approved_by,
             f"NCR-{ncr_id}", _now(ts)))
    elif disposition == "REWORK":
        u = _unit(conn, unit_id)
        conn.execute(
            "INSERT INTO op_record (wo_id, unit_id, seq, action, qty, op_id, reason, ts) "
            "VALUES (?,?,?,'REWORK_ENTRY',?,?,?,?)",
            (u["wo_id"], unit_id, rework_to_seq, u["lot_qty"] or 1, approved_by,
             f"NCR-{ncr_id}", _now(ts)))
        conn.execute("UPDATE unit SET current_seq=?, status='IN_PROCESS' WHERE unit_id=?",
                     (rework_to_seq, unit_id))
    audit(conn, approved_by, None, f"NCR_{disposition}", unit_id,
          f"ncr={ncr_id} rework_to={rework_to_seq}", _now(ts))


# --------------------------------------------------------------------------
# the invariant
# --------------------------------------------------------------------------

def conservation_report(conn, wo_id: str | None = None) -> list[dict]:
    """started == completed + scrapped + in_process, per (work order, operation).

    Counted from the append-only ledger rather than from a status column, because
    a status column is a cache and this is the thing the cache is supposed to
    agree with. `in_process` is the residual: units that started this pass and have
    neither completed it nor been scrapped.
    """
    where = "WHERE o.wo_id = ?" if wo_id else ""
    args = (wo_id,) if wo_id else ()
    rows = conn.execute(f"""
        SELECT o.wo_id, o.seq,
               SUM(CASE WHEN o.action='START'    THEN o.qty ELSE 0 END) AS started,
               SUM(CASE WHEN o.action='COMPLETE' THEN o.qty ELSE 0 END) AS completed,
               SUM(CASE WHEN o.action='SCRAP'    THEN o.qty ELSE 0 END) AS scrapped,
               SUM(CASE WHEN o.action='REWORK_ENTRY' THEN o.qty ELSE 0 END) AS reworked
        FROM op_record o {where}
        GROUP BY o.wo_id, o.seq ORDER BY o.wo_id, o.seq
    """, args).fetchall()

    # A pass that ended in a NONCONFORMANCE is neither completed nor scrapped --
    # it was dispositioned. Leaving that category out of the identity is a real
    # bug and it showed up exactly where it should: reworked units started op 50
    # twice and completed it once, so op 50 reported a phantom in-process balance
    # of 2 on units that were sitting on the shipping dock. The identity is
    #     started = completed + scrapped + nonconformances + in_process
    # and the NCR term is what makes a reworked unit balance.
    ncr_where = "WHERE u.wo_id = ?" if wo_id else ""
    ncrs = {
        (r["wo_id"], r["seq"]): r["n"]
        for r in conn.execute(f"""
            SELECT u.wo_id, n.seq, COUNT(*) AS n
            FROM ncr n JOIN unit u ON u.unit_id = n.unit_id {ncr_where}
            GROUP BY u.wo_id, n.seq
        """, args).fetchall()
    }

    out = []
    for r in rows:
        # A rework entry re-starts the unit at that operation, so it adds to the
        # started side of the ledger. Omitting it is the classic way to make a
        # reworked unit look like a conservation violation.
        started = r["started"]
        n_ncr = ncrs.get((r["wo_id"], r["seq"]), 0)
        balance = started - (r["completed"] + r["scrapped"] + n_ncr)
        out.append({
            "wo_id": r["wo_id"], "seq": r["seq"],
            "started": started, "completed": r["completed"],
            "scrapped": r["scrapped"], "reworked": r["reworked"],
            "nonconformances": n_ncr,
            "in_process": balance,
            "violates": bool(balance < -1e-9),
        })
    return out


def assert_conservation(conn, wo_id: str | None = None) -> None:
    bad = [r for r in conservation_report(conn, wo_id) if r["violates"]]
    if bad:
        raise ConservationError(
            f"{len(bad)} operation(s) completed or scrapped more than was started: "
            + ", ".join(f"{b['wo_id']}/op{b['seq']} balance {b['in_process']}" for b in bad))
