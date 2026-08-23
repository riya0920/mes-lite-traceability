"""Finite-capacity scheduling, a shift calendar, and the transaction boundary
that makes concurrency safe.

===========================================================================
PART 1 -- FINITE CAPACITY, and why infinite-capacity planning fails
===========================================================================

`work_center.capacity` has existed since pass 1 and nothing reads it. That is not
a missing feature so much as a missing *constraint*: an infinite-capacity plan
schedules every job at its earliest start, which means it schedules three jobs
onto one machine at 08:00 and reports a completion date that assumes they all
ran. The plan is not optimistic by a little -- it is arithmetically impossible,
and it fails in exactly the way that erodes trust in a scheduling system, because
the dates look reasonable right up until they are all missed.

Finite capacity forward-scheduling: each operation starts at the later of

    (a) when its predecessor operation on the same unit finished, and
    (b) when a machine at its work centre is next free

That second clause is the whole difference, and it produces QUEUE TIME, which is
usually most of a manufacturing lead time. A plant's lead time is dominated by
waiting, not by cutting -- typical run-time-to-lead-time ratios are under 10% --
so an infinite-capacity plan understates lead time by roughly the factor that
matters most.

DISPATCH RULES change the answer and none of them dominates:

  FIFO   fair, predictable, easy to explain to a shop floor. Poor average
         flow time.
  SPT    shortest processing time first. Provably minimises mean flow time,
         and STARVES long jobs -- a big job can sit behind an endless stream of
         small ones.
  EDD    earliest due date. Minimises maximum lateness, at the cost of mean
         flow time.
  CR     critical ratio: time remaining / work remaining. Adaptive, and it
         responds to a job falling behind.

All four are implemented and compared, because "which rule" is a real decision
with a measurable answer that depends on what the plant is being judged on.

===========================================================================
PART 2 -- SHIFT CALENDARS
===========================================================================

A schedule that runs through the night on a two-shift plant is a fiction. The
calendar is not decoration: on a two-shift operation, an eight-hour job started
at 14:00 does not finish at 22:00, it finishes at 06:00 the next morning, and
the difference compounds through every downstream operation.

===========================================================================
PART 3 -- THE TRANSACTION BOUNDARY
===========================================================================

The README is explicit that the double-completion guard is a read-then-write
without a transaction boundary, and that calling it race-safe would be an
overclaim. It is right. `complete_operation` reads history, decides, then
inserts -- and two threads can both read "not yet completed" before either
writes.

The fix is not a lock in Python. It is:

  1. BEGIN IMMEDIATE, which takes SQLite's write lock at the start of the
     transaction rather than on first write, so the read is inside the lock
  2. a UNIQUE constraint that makes the invariant true in the SCHEMA, so it holds
     even against a client that forgets the transaction

The second is the one that matters. Application-level checks protect against
code that goes through them; a constraint protects against everything, including
the migration script somebody runs at 2 a.m.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# shift calendar
# ---------------------------------------------------------------------------

@dataclass
class ShiftCalendar:
    """Working minutes per day. Times are minutes from midnight.

    Two shifts by default, with a break in each. Breaks matter more than they
    look: a 30-minute break inside an 8-hour job adds 30 minutes to its
    completion, and on a line where every operation crosses a break that is
    hours per unit across the routing.
    """
    shifts: list = field(default_factory=lambda: [(6 * 60, 14 * 60),
                                                  (14 * 60, 22 * 60)])
    breaks: list = field(default_factory=lambda: [(10 * 60, 10 * 60 + 20),
                                                  (18 * 60, 18 * 60 + 20)])
    working_days: tuple = (0, 1, 2, 3, 4)          # Mon-Fri

    def _windows(self, day: int) -> list[tuple[float, float]]:
        if day % 7 not in self.working_days:
            return []
        out = []
        for s, e in self.shifts:
            cur = [(s, e)]
            for bs, be in self.breaks:
                nxt = []
                for a, b in cur:
                    if be <= a or bs >= b:
                        nxt.append((a, b))
                    else:
                        if a < bs:
                            nxt.append((a, bs))
                        if be < b:
                            nxt.append((be, b))
                cur = nxt
            out.extend(cur)
        return sorted(out)

    def next_working_minute(self, t: float) -> float:
        """The first working minute at or after `t` (absolute minutes)."""
        day = int(t // 1440)
        for d in range(day, day + 21):
            for a, b in self._windows(d):
                lo, hi = d * 1440 + a, d * 1440 + b
                if t < lo:
                    return float(lo)
                if lo <= t < hi:
                    return float(t)
        return float(t)                                       # pragma: no cover

    def add_working_minutes(self, start: float, minutes: float) -> float:
        """Advance `minutes` of WORK from `start`, skipping non-working time."""
        t = self.next_working_minute(start)
        remaining = float(minutes)
        for _ in range(2000):
            day = int(t // 1440)
            placed = False
            for a, b in self._windows(day):
                lo, hi = day * 1440 + a, day * 1440 + b
                if lo <= t < hi:
                    avail = hi - t
                    if remaining <= avail:
                        return t + remaining
                    remaining -= avail
                    t = hi
                    placed = True
                    break
            if not placed:
                t = self.next_working_minute(t + 1)
        raise RuntimeError("could not place the work inside 2000 windows")

    def elapsed_vs_working(self, start: float, minutes: float) -> dict:
        end = self.add_working_minutes(start, minutes)
        return {"start": start, "end": end, "working_minutes": minutes,
                "elapsed_minutes": end - start,
                "calendar_inflation": (end - start) / max(minutes, 1e-9)}


# ---------------------------------------------------------------------------
# finite-capacity scheduling
# ---------------------------------------------------------------------------

@dataclass
class Job:
    job_id: str
    ops: list                    # [(seq, work_centre, minutes)]
    due: float
    released: float = 0.0


DISPATCH_RULES = ("FIFO", "SPT", "EDD", "CR")


def schedule(jobs: list, capacity: dict, rule: str = "FIFO",
             calendar: ShiftCalendar | None = None) -> dict:
    """Forward finite-capacity schedule under one dispatch rule.

    Non-delay: a machine never idles while work is waiting for it. That is the
    standard simplification and it is not always optimal -- deliberately holding a
    machine for a job about to arrive can beat starting a long one now -- but
    non-delay schedules are what a shop floor actually produces, because an
    operator with a free machine and a queue starts something.
    """
    if rule not in DISPATCH_RULES:
        raise ValueError(f"unknown dispatch rule {rule!r}")
    cal = calendar
    # Per work centre: a list of "next free" times, one entry per machine.
    free = {wc: [0.0] * max(int(n), 1) for wc, n in capacity.items()}
    ready = {j.job_id: float(j.released) for j in jobs}
    remaining = {j.job_id: sum(m for _, _, m in j.ops) for j in jobs}
    pending = {j.job_id: list(j.ops) for j in jobs}
    due = {j.job_id: j.due for j in jobs}
    result: dict[str, list] = {j.job_id: [] for j in jobs}
    queue_time = {j.job_id: 0.0 for j in jobs}

    def priority(jid, op, now):
        seq, wc, mins = op
        if rule == "FIFO":
            return ready[jid]
        if rule == "SPT":
            return mins
        if rule == "EDD":
            return due[jid]
        # Critical ratio: <1 means already late. Lower is more urgent, and it is
        # the only rule here that reacts to a job falling behind.
        slack = due[jid] - now
        return slack / max(remaining[jid], 1e-9)

    guard = 0
    while any(pending.values()) and guard < 100000:
        guard += 1
        # Candidates: each job's next operation, if its predecessor is done.
        cands = [(jid, ops[0]) for jid, ops in pending.items() if ops]
        if not cands:
            break
        # Earliest time anything could start, so the rule chooses among jobs
        # actually available rather than among all of them.
        starts = {}
        for jid, op in cands:
            _, wc, _ = op
            starts[jid] = max(ready[jid], min(free.get(wc, [0.0])))
        now = min(starts.values())
        available = [(jid, op) for jid, op in cands
                     if starts[jid] <= now + 1e-9]
        jid, op = min(available, key=lambda c: priority(c[0], c[1], now))

        seq, wc, mins = op
        machines = free.setdefault(wc, [0.0])
        m_idx = min(range(len(machines)), key=lambda i: machines[i])
        start = max(ready[jid], machines[m_idx])
        if cal is not None:
            end = cal.add_working_minutes(start, mins)
            start = cal.next_working_minute(start)
        else:
            end = start + mins
        queue_time[jid] += max(start - ready[jid], 0.0)
        machines[m_idx] = end
        ready[jid] = end
        remaining[jid] -= mins
        result[jid].append({"seq": seq, "wc": wc, "machine": m_idx,
                            "start": start, "end": end, "minutes": mins})
        pending[jid].pop(0)

    finish = {j: (ops[-1]["end"] if ops else 0.0) for j, ops in result.items()}
    flow = [finish[j.job_id] - j.released for j in jobs]
    late = [max(finish[j.job_id] - j.due, 0.0) for j in jobs]
    work = {j.job_id: sum(m for _, _, m in j.ops) for j in jobs}
    return {
        "rule": rule, "schedule": result, "finish": finish,
        "makespan": max(finish.values()) if finish else 0.0,
        "mean_flow_time": sum(flow) / max(len(flow), 1),
        "max_lateness": max(late) if late else 0.0,
        "n_late": sum(1 for x in late if x > 0),
        "total_tardiness": sum(late),
        "mean_queue_time": sum(queue_time.values()) / max(len(queue_time), 1),
        "queue_share_of_flow": (sum(queue_time.values())
                                / max(sum(flow), 1e-9)),
        "work_content": work,
    }


def infinite_capacity(jobs: list, calendar: ShiftCalendar | None = None) -> dict:
    """The plan a system without a capacity constraint produces.

    Every job starts as soon as its own predecessor finishes, and machines are
    assumed infinitely available. Kept so the finite-capacity result has
    something to be compared against -- the gap between them is the queue time an
    infinite-capacity planner promises away.
    """
    finish = {}
    for j in jobs:
        t = float(j.released)
        for _, _, m in j.ops:
            t = calendar.add_working_minutes(t, m) if calendar else t + m
        finish[j.job_id] = t
    flow = [finish[j.job_id] - j.released for j in jobs]
    late = [max(finish[j.job_id] - j.due, 0.0) for j in jobs]
    return {"rule": "INFINITE CAPACITY (not a plan)", "finish": finish,
            "makespan": max(finish.values()) if finish else 0.0,
            "mean_flow_time": sum(flow) / max(len(flow), 1),
            "max_lateness": max(late) if late else 0.0,
            "n_late": sum(1 for x in late if x > 0),
            "total_tardiness": sum(late)}


def compare_rules(jobs: list, capacity: dict,
                  calendar: ShiftCalendar | None = None) -> list[dict]:
    rows = []
    for rule in DISPATCH_RULES:
        r = schedule(jobs, capacity, rule, calendar)
        rows.append({k: r[k] for k in
                     ("rule", "makespan", "mean_flow_time", "max_lateness",
                      "n_late", "total_tardiness", "mean_queue_time",
                      "queue_share_of_flow")})
    return rows


# ---------------------------------------------------------------------------
# the transaction boundary
# ---------------------------------------------------------------------------

UNIQUE_COMPLETION_INDEX = """
-- One COMPLETE per (unit, seq, pass). `pass_no` is the rework generation, so a
-- legitimately reworked unit can complete the same operation again while a
-- double-completion within one pass is impossible.
--
-- This is the guarantee that survives a client which forgets the transaction.
-- An application-level check protects code that goes through it; a UNIQUE index
-- protects against everything, including the migration script somebody runs at
-- 2 a.m.
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_complete_per_pass
ON op_record (unit_id, seq, pass_no)
WHERE action = 'COMPLETE';
"""


def add_pass_column(conn: sqlite3.Connection) -> dict:
    """Add `pass_no` and the unique index. Idempotent."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(op_record)")}
    added = False
    if "pass_no" not in cols:
        conn.execute("ALTER TABLE op_record ADD COLUMN pass_no INTEGER DEFAULT 0")
        added = True
    # Backfill: a pass is the count of rework entries at or before this seq.
    conn.execute("""
        UPDATE op_record SET pass_no = (
          SELECT COUNT(*) FROM op_record r2
          WHERE r2.unit_id = op_record.unit_id
            AND r2.seq <= op_record.seq
            AND r2.action = 'REWORK_ENTRY'
            AND r2.rec_id < op_record.rec_id)
    """)
    conn.executescript(UNIQUE_COMPLETION_INDEX)
    conn.commit()
    return {"column_added": added, "index": "ux_one_complete_per_pass"}


def complete_atomic(conn: sqlite3.Connection, unit_id: str, seq: int,
                    op_id: str, wc_id: str, qty: float = 1.0,
                    ts: str | None = None) -> dict:
    """A completion inside BEGIN IMMEDIATE, relying on the unique index.

    BEGIN IMMEDIATE takes the write lock at the START of the transaction rather
    than on the first write. Without it, SQLite runs the read in a deferred
    (shared) transaction and both threads can read "not yet completed" before
    either upgrades to write -- and the loser gets SQLITE_BUSY at COMMIT, which
    is far too late, because by then it has already decided the completion was
    valid.
    """
    import execution as ex

    row = conn.execute("SELECT wo_id FROM unit WHERE unit_id=?",
                       (unit_id,)).fetchone()
    if row is None:
        return {"ok": False, "reason": "unknown unit"}
    wo_id = row[0]
    pass_no = conn.execute(
        "SELECT COUNT(*) FROM op_record WHERE unit_id=? AND seq<=? "
        "AND action='REWORK_ENTRY'", (unit_id, seq)).fetchone()[0]
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO op_record (wo_id, unit_id, seq, op_id, wc_id, action, "
            "qty, ts, pass_no) VALUES (?,?,?,?,?,'COMPLETE',?,?,?)",
            (wo_id, unit_id, seq, op_id, wc_id, qty, ts or ex._now(), pass_no))
        conn.execute("COMMIT")
        return {"ok": True, "pass_no": pass_no}
    except sqlite3.IntegrityError as e:
        conn.execute("ROLLBACK")
        return {"ok": False, "reason": "duplicate completion for this pass",
                "detail": str(e)[:100], "pass_no": pass_no}
    except sqlite3.OperationalError as e:
        conn.execute("ROLLBACK")
        return {"ok": False, "reason": "locked", "detail": str(e)[:100]}


def race_two_operators(db_path, unit_id: str, seq: int, op_id: str, wc_id: str,
                       n_threads: int = 8) -> dict:
    """Two operators complete the same operation at the same instant.

    A barrier is used so the threads genuinely contend rather than running one
    after another -- without it the test passes for the wrong reason, which is
    the usual outcome of a concurrency test written in a hurry.
    """
    barrier = threading.Barrier(n_threads)
    results: list[dict] = []
    lock = threading.Lock()

    def worker():
        conn = sqlite3.connect(db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        try:
            barrier.wait(timeout=20)
            r = complete_atomic(conn, unit_id, seq, op_id, wc_id)
        except Exception as e:                                # noqa: BLE001
            r = {"ok": False, "reason": type(e).__name__, "detail": str(e)[:100]}
        finally:
            conn.close()
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    won = sum(1 for r in results if r.get("ok"))
    return {"n_threads": n_threads, "winners": won,
            "losers": len(results) - won,
            "exactly_one_won": won == 1,
            "loss_reasons": sorted({r.get("reason") for r in results
                                    if not r.get("ok")}),
            "results": results}
