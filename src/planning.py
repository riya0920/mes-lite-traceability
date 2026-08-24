"""Finite-capacity planning against the routing that is actually in the database.

`scheduling.schedule()` takes a synthetic job list of `(seq, work_centre,
minutes)` triples. The database it sits next to has, and has always had,
`operation.std_setup_s`, `operation.cert_required`, `work_center.capacity` and a
`certification` table. The scheduler used none of it, so its plans were built
from a routing that resembled the real one rather than being it.

Three things are added here, and each of them can only make a schedule worse --
which is the point. A planner whose constraints are optional produces a promise
nobody can keep:

  * SEQUENCE-DEPENDENT SETUP. `std_setup_s` is the cost of setting the machine up
    from scratch. Running a second unit of the same SKU behind the first costs
    nothing extra; switching SKUs costs the full setup, and switching between
    unrelated families costs more than switching within one. So the setup a job
    incurs depends on what the machine did *before* it, which is what makes the
    dispatch rule's choice matter beyond its own objective.

  * OPERATORS. A machine is not a resource on its own; a machine plus somebody
    certified to run it is. The certification table already says who can do what,
    and five operators cover three certifications unevenly on purpose -- OP-05
    holds none at all.

  * BACKWARD SCHEDULING. Forward scheduling answers *when will it be done*.
    Backward answers *when must this be released*, and a release date in the past
    is the only output that tells you the promise is already broken.

`schedule_finite` with no setup matrix and unlimited operators reproduces
`scheduling.schedule()` operation for operation. That equivalence is asserted in
the tests, and it is what makes the difference between them attributable to the
constraints rather than to a rewrite.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import scheduling as S

_EPS = 1e-9


# ---------------------------------------------------------------------------
# jobs from the real routing
# ---------------------------------------------------------------------------

@dataclass
class Op:
    seq: int
    wc: str
    run_min: float
    setup_min: float = 0.0
    cert: str | None = None
    sku: str = ""


@dataclass
class PlanJob:
    job_id: str
    sku: str
    qty: int
    ops: list
    due: float
    released: float = 0.0

    def as_scheduling_job(self) -> S.Job:
        """The lossy view the old scheduler takes: run time only."""
        return S.Job(job_id=self.job_id,
                     ops=[(o.seq, o.wc, o.run_min) for o in self.ops],
                     due=self.due, released=self.released)


def routing_of(conn: sqlite3.Connection, sku: str) -> list:
    rows = conn.execute(
        "SELECT seq, wc_id, std_setup_s, std_run_s, cert_required "
        "FROM operation WHERE sku=? ORDER BY seq", (sku,)).fetchall()
    return [Op(seq=r["seq"], wc=r["wc_id"], run_min=r["std_run_s"] / 60.0,
               setup_min=r["std_setup_s"] / 60.0, cert=r["cert_required"],
               sku=sku) for r in rows]


def capacity_of(conn: sqlite3.Connection) -> dict:
    return {r["wc_id"]: int(r["capacity"])
            for r in conn.execute("SELECT wc_id, capacity FROM work_center")}


def jobs_from_db(conn: sqlite3.Connection, orders: list) -> list:
    """`orders` is [(job_id, sku, qty, due_min, released_min), ...].

    Run time scales with quantity; setup does not. That asymmetry is the whole
    economics of batching, and a planner that multiplies both by qty makes large
    batches look worse than they are.
    """
    out = []
    for job_id, sku, qty, due, released in orders:
        ops = []
        for o in routing_of(conn, sku):
            ops.append(Op(seq=o.seq, wc=o.wc, run_min=o.run_min * qty,
                          setup_min=o.setup_min, cert=o.cert, sku=sku))
        if not ops:
            raise ValueError(f"no routing for sku {sku!r}")
        out.append(PlanJob(job_id=job_id, sku=sku, qty=qty, ops=ops,
                           due=float(due), released=float(released)))
    return out


# ---------------------------------------------------------------------------
# setup matrix
# ---------------------------------------------------------------------------

FAMILY = {"BRKT-100": "bracket", "PLATE-200": "plate"}


@dataclass
class SetupMatrix:
    """Sequence-dependent setup, expressed as a multiplier on `std_setup_s`.

    Not an arbitrary matrix of numbers: the standard setup is already in the
    database and is the honest starting point, so this scales it rather than
    replacing it. Three cases, and they are ordered the way a shop floor orders
    them --

        same SKU        no changeover at all
        same family     a partial change: fixture stays, program changes
        across family   the full documented setup, or more

    `cross_family` above 1.0 says switching between a bracket and a plate costs
    more than the standard setup, which is the normal case: the standard is
    usually measured within a family.
    """
    same_sku: float = 0.0
    same_family: float = 0.55
    cross_family: float = 1.25
    first_of_shift: float = 1.0
    families: dict = field(default_factory=lambda: dict(FAMILY))

    def minutes(self, prev_sku: str | None, op: Op) -> float:
        if prev_sku is None:
            return op.setup_min * self.first_of_shift
        if prev_sku == op.sku:
            return op.setup_min * self.same_sku
        if self.families.get(prev_sku) == self.families.get(op.sku):
            return op.setup_min * self.same_family
        return op.setup_min * self.cross_family


# ---------------------------------------------------------------------------
# operators
# ---------------------------------------------------------------------------

@dataclass
class OperatorPool:
    """Who is available, and what they are certified for.

    `attends` decides whether an operator is held for the whole operation or
    only for the setup. Both are real: a CNC cell is set up attended and then
    runs unattended, and a manual weld is attended throughout. It changes the
    answer a lot, so it is a parameter rather than a buried assumption.
    """
    certs: dict                       # op_id -> set of certs
    attends: str = "all"              # "all" | "setup"

    @classmethod
    def from_db(cls, conn: sqlite3.Connection, attends: str = "all"):
        certs: dict = {r["op_id"]: set()
                       for r in conn.execute("SELECT op_id FROM operator")}
        for r in conn.execute("SELECT op_id, cert FROM certification"):
            certs.setdefault(r["op_id"], set()).add(r["cert"])
        return cls(certs=certs, attends=attends)

    def qualified(self, cert: str | None) -> list:
        if cert is None:
            return sorted(self.certs)
        return sorted(o for o, c in self.certs.items() if cert in c)

    def coverage(self) -> dict:
        """How many people can do each thing. A cert with one holder is a plan
        with a single point of failure in it, and it does not show up anywhere in
        a capacity number."""
        all_certs = sorted({c for cs in self.certs.values() for c in cs})
        return {c: len(self.qualified(c)) for c in all_certs}


class _Unlimited(OperatorPool):
    """Operators are never the constraint. Used for the reconciliation against
    the old scheduler, and as the baseline the operator cost is measured from."""

    def __init__(self):
        super().__init__(certs={}, attends="all")

    def qualified(self, cert):        # noqa: D401
        return ["*"]


UNLIMITED = _Unlimited()


# ---------------------------------------------------------------------------
# forward finite-capacity schedule
# ---------------------------------------------------------------------------

def schedule_finite(jobs: list, capacity: dict, rule: str = "FIFO",
                    calendar=None, setups: SetupMatrix | None = None,
                    operators: OperatorPool | None = None) -> dict:
    """Non-delay forward schedule over machines AND operators, with setups.

    Non-delay is inherited from `scheduling.schedule()` deliberately: a machine
    never idles while work is waiting for it. It is not always optimal -- holding
    a machine for a job about to arrive can beat starting a long one now -- but
    it is what a shop floor produces, and changing it here would confound the
    effect being measured with a change of policy.
    """
    if rule not in S.DISPATCH_RULES:
        raise ValueError(f"unknown dispatch rule {rule!r}")
    ops_pool = operators or UNLIMITED
    unlimited = isinstance(ops_pool, _Unlimited)

    free = {wc: [0.0] * max(int(n), 1) for wc, n in capacity.items()}
    last_sku = {wc: [None] * max(int(n), 1) for wc, n in capacity.items()}
    op_free = {} if unlimited else {o: 0.0 for o in ops_pool.certs}

    ready = {j.job_id: float(j.released) for j in jobs}
    remaining = {j.job_id: sum(o.run_min for o in j.ops) for j in jobs}
    pending = {j.job_id: list(j.ops) for j in jobs}
    due = {j.job_id: j.due for j in jobs}
    result: dict = {j.job_id: [] for j in jobs}
    queue_time = {j.job_id: 0.0 for j in jobs}
    setup_total = 0.0
    operator_wait = 0.0
    uncovered = []

    def priority(jid, op, now):
        if rule == "FIFO":
            return ready[jid]
        if rule == "SPT":
            return op.run_min
        if rule == "EDD":
            return due[jid]
        return (due[jid] - now) / max(remaining[jid], 1e-9)

    def machine_ready(jid, wc, op):
        """Earliest a machine at `wc` could finish setting up for `op`.

        Chooses the machine that can FINISH the setup soonest, not the one that
        is free soonest. Those differ exactly when a busier machine already has
        the right SKU on it, and preferring the idle machine there is how a
        planner spends an hour of setup to save five minutes of waiting.
        """
        machines = free.setdefault(wc, [0.0])
        skus = last_sku.setdefault(wc, [None] * len(machines))
        best, best_t, best_s = 0, None, 0.0
        for i, t in enumerate(machines):
            s = setups.minutes(skus[i], op) if setups else 0.0
            start = max(ready[jid], t)
            if calendar is not None:
                fin = calendar.add_working_minutes(
                    calendar.next_working_minute(start), s)
            else:
                fin = start + s
            if best_t is None or fin < best_t - _EPS:
                best, best_t, best_s = i, fin, s
        return best, best_t, best_s

    guard = 0
    while any(pending.values()) and guard < 200000:
        guard += 1
        cands = [(jid, ops[0]) for jid, ops in pending.items() if ops]
        if not cands:
            break
        starts = {}
        for jid, op in cands:
            starts[jid] = max(ready[jid], min(free.get(op.wc, [0.0])))
        now = min(starts.values())
        available = [(jid, op) for jid, op in cands if starts[jid] <= now + _EPS]
        jid, op = min(available, key=lambda c: priority(c[0], c[1], now))

        m_idx, setup_end, setup_min = machine_ready(jid, op.wc, op)
        machines = free[op.wc]
        setup_start = max(ready[jid], machines[m_idx])
        if calendar is not None:
            setup_start = calendar.next_working_minute(setup_start)

        # An operator must be free for the setup, and for the run too when the
        # pool attends throughout.
        chosen_op = "*"
        if not unlimited:
            who = ops_pool.qualified(op.cert)
            if not who:
                # Nobody on the payroll can do this. Not a scheduling delay --
                # an unschedulable operation, and reporting it as an infinite
                # wait would bury it in a mean.
                uncovered.append({"job": jid, "seq": op.seq, "cert": op.cert})
                pending[jid].pop(0)
                continue
            chosen_op = min(who, key=lambda o: op_free[o])
            avail = op_free[chosen_op]
            if avail > setup_start + _EPS:
                wait = avail - setup_start
                operator_wait += wait
                setup_start = avail
                if calendar is not None:
                    setup_start = calendar.next_working_minute(setup_start)

        if calendar is not None:
            s_end = calendar.add_working_minutes(setup_start, setup_min)
            end = calendar.add_working_minutes(s_end, op.run_min)
        else:
            s_end = setup_start + setup_min
            end = s_end + op.run_min

        setup_total += setup_min
        queue_time[jid] += max(setup_start - ready[jid], 0.0)
        machines[m_idx] = end
        last_sku[op.wc][m_idx] = op.sku
        if not unlimited:
            op_free[chosen_op] = end if ops_pool.attends == "all" else s_end
        ready[jid] = end
        remaining[jid] -= op.run_min
        result[jid].append({"seq": op.seq, "wc": op.wc, "machine": m_idx,
                            "setup_start": setup_start, "start": s_end,
                            "end": end, "setup_minutes": setup_min,
                            "minutes": op.run_min, "operator": chosen_op,
                            "cert": op.cert})
        pending[jid].pop(0)

    finish = {j: (ops[-1]["end"] if ops else 0.0) for j, ops in result.items()}
    flow = [finish[j.job_id] - j.released for j in jobs]
    late = [max(finish[j.job_id] - j.due, 0.0) for j in jobs]
    run_total = sum(o.run_min for j in jobs for o in j.ops)
    return {
        "rule": rule, "schedule": result, "finish": finish,
        "makespan": max(finish.values()) if finish else 0.0,
        "mean_flow_time": sum(flow) / max(len(flow), 1),
        "max_lateness": max(late) if late else 0.0,
        "n_late": sum(1 for x in late if x > 0),
        "total_tardiness": sum(late),
        "mean_queue_time": sum(queue_time.values()) / max(len(queue_time), 1),
        "queue_share_of_flow": sum(queue_time.values()) / max(sum(flow), 1e-9),
        "setup_minutes": setup_total,
        "run_minutes": run_total,
        "setup_share_of_work": setup_total / max(setup_total + run_total, 1e-9),
        "operator_wait_minutes": operator_wait,
        "uncovered_operations": uncovered,
        "operators_used": sorted({r["operator"] for rs in result.values()
                                  for r in rs}),
    }


# ---------------------------------------------------------------------------
# backward scheduling
# ---------------------------------------------------------------------------

def backward_from_due(job: PlanJob, calendar=None,
                      setups: SetupMatrix | None = None,
                      prev_sku: str | None = None) -> dict:
    """Latest release date for one job, walking its routing backwards.

    INFINITE CAPACITY on purpose. A backward pass over a contended shop is a
    different and much harder problem, and pretending otherwise would produce a
    release date that looks authoritative and is not. What this gives is a bound:
    a job that cannot make its date even with every machine free will certainly
    not make it with the machines it actually has, and that answer is available
    before anybody looks at a queue.
    """
    t = float(job.due)
    rows = []
    for op in reversed(job.ops):
        s = setups.minutes(prev_sku if op is job.ops[0] else None, op) if setups else 0.0
        end = t
        if calendar is not None:
            start = calendar.subtract_working_minutes(end, op.run_min)
            setup_start = calendar.subtract_working_minutes(start, s)
        else:
            start = end - op.run_min
            setup_start = start - s
        rows.append({"seq": op.seq, "wc": op.wc, "latest_setup_start": setup_start,
                     "latest_start": start, "latest_end": end,
                     "setup_minutes": s, "minutes": op.run_min})
        t = setup_start
    rows.reverse()
    release = rows[0]["latest_setup_start"] if rows else job.due
    slack = release - job.released
    return {"job_id": job.job_id, "sku": job.sku, "due": job.due,
            "latest_release": release, "released": job.released,
            "slack_minutes": slack, "feasible": slack >= -_EPS,
            "critical_path_minutes": job.due - release,
            "schedule": rows}


def backward_all(jobs: list, calendar=None, setups: SetupMatrix | None = None) -> dict:
    rows = [backward_from_due(j, calendar, setups) for j in jobs]
    infeasible = [r for r in rows if not r["feasible"]]
    return {"jobs": rows, "n_infeasible": len(infeasible),
            "worst_slack": min((r["slack_minutes"] for r in rows), default=0.0),
            "infeasible": [r["job_id"] for r in infeasible]}


def reconcile_forward_backward(jobs: list, capacity: dict, rule: str = "EDD",
                               calendar=None,
                               setups: SetupMatrix | None = None,
                               operators: OperatorPool | None = None) -> dict:
    """What the infinite-capacity backward promise costs once capacity is real.

    The gap between the two is the number a planning system exists to produce and
    the number it is most tempted to hide: backward scheduling says a job needs
    N minutes of lead time, the finite-capacity forward pass says it needs more,
    and the difference is queue -- time the job spends waiting for a machine that
    no routing document mentions.
    """
    back = backward_all(jobs, calendar, setups)
    fwd = schedule_finite(jobs, capacity, rule, calendar, setups, operators)
    rows = []
    for j, b in zip(jobs, back["jobs"]):
        planned = b["critical_path_minutes"]
        actual = fwd["finish"][j.job_id] - j.released
        rows.append({"job_id": j.job_id,
                     "backward_lead_time": planned,
                     "forward_lead_time": actual,
                     "queue_minutes": actual - planned,
                     "inflation": actual / max(planned, 1e-9)})
    return {"backward": back, "forward": {k: v for k, v in fwd.items()
                                          if k != "schedule"},
            "per_job": rows,
            "mean_inflation": sum(r["inflation"] for r in rows) / max(len(rows), 1)}
