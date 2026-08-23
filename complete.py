"""SE-2, the rest: real concurrency, finite-capacity scheduling, a shift
calendar, integration with DATA-1 and ML-1, electronic signatures, a scale test,
and an operator terminal.

    python complete.py
    python complete.py --quick
    python complete.py --report-only

Mapping to the README's not-built list:

  1  no concurrency testing; the guard would race        -> stage 1
  2  no scheduling or capacity, no shift calendar        -> stage 2
  4  no UI                                               -> stage 6
  5  no integration with DATA-1's states or ML-1's alarms-> stage 3
  7  audit is AS9100-AWARE: no e-signature, no retention -> stage 4
  8  33 units; the numbers change shape at millions      -> stage 5
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import time

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import execution as ex  # noqa: E402
import generate  # noqa: E402
import model  # noqa: E402
import scheduling as SCH  # noqa: E402
import signature as SIG  # noqa: E402
import terminal as TERM  # noqa: E402

OUT = ROOT / "out"
DOCS = ROOT / "docs"
QUICK = "--quick" in sys.argv


def _fresh(name: str) -> pathlib.Path:
    p = OUT / name
    for suffix in ("", "-wal", "-shm"):
        q = pathlib.Path(str(p) + suffix)
        if q.exists():
            q.unlink()
    return p


# ---------------------------------------------------------------------------
# 1. concurrency
# ---------------------------------------------------------------------------

def stage_concurrency() -> dict:
    """Two operators complete the same operation at the same instant.

    The README refused to call the existing guard race-safe, and was right: it
    is a read-then-write with no transaction boundary. This adds the boundary and
    the schema constraint, then actually races it.
    """
    db = _fresh("concurrency.db")
    conn = model.create(db)
    conn.execute("PRAGMA journal_mode=WAL")
    generate.seed_definitions(conn); generate.seed_lots(conn)
    conn.execute("INSERT INTO work_order VALUES ('WO-9001','BRKT-100',10,"
                 "'IN_PROCESS','2026-07-01T06:00:00+00:00')")
    conn.execute("INSERT INTO unit (unit_id, wo_id, serial, lot_qty, status, "
                 "current_seq) VALUES ('WO-9001-U1','WO-9001','SN9001',NULL,"
                 "'IN_PROCESS',NULL)")
    conn.commit()
    ex.start_operation(conn, "WO-9001-U1", 10, "OP-01", "WC-CUT")
    conn.commit()

    migration = SCH.add_pass_column(conn)
    conn.close()

    n = 8
    race = SCH.race_two_operators(str(db), "WO-9001-U1", 10, "OP-01", "WC-CUT",
                                  n_threads=n)

    # The counterfactual: the same race WITHOUT the unique index, to show the
    # guard alone is not enough. A separate database, because dropping the index
    # on the live one would leave the artefact in a state that contradicts the
    # report.
    db2 = _fresh("concurrency_noindex.db")
    conn2 = model.create(db2)
    conn2.execute("PRAGMA journal_mode=WAL")
    generate.seed_definitions(conn2); generate.seed_lots(conn2)
    conn2.execute("INSERT INTO work_order VALUES ('WO-9002','BRKT-100',10,"
                  "'IN_PROCESS','2026-07-01T06:00:00+00:00')")
    conn2.execute("INSERT INTO unit (unit_id, wo_id, serial, lot_qty, status, "
                  "current_seq) VALUES ('WO-9002-U1','WO-9002','SN9002',NULL,"
                  "'IN_PROCESS',NULL)")
    conn2.commit()
    ex.start_operation(conn2, "WO-9002-U1", 10, "OP-01", "WC-CUT")
    conn2.execute("ALTER TABLE op_record ADD COLUMN pass_no INTEGER DEFAULT 0")
    conn2.commit()
    conn2.close()
    race_unguarded = SCH.race_two_operators(
        str(db2), "WO-9002-U1", 10, "OP-01", "WC-CUT", n_threads=n)

    return {"migration": migration, "guarded": race,
            "unguarded": {k: race_unguarded[k] for k in
                          ("n_threads", "winners", "losers", "exactly_one_won")},
            "why": ("BEGIN IMMEDIATE takes the write lock at the START of the "
                    "transaction so the read happens inside it; the UNIQUE index "
                    "makes the invariant true in the schema, which is what holds "
                    "against a client that forgets the transaction")}


# ---------------------------------------------------------------------------
# 2. finite-capacity scheduling
# ---------------------------------------------------------------------------

def stage_scheduling() -> dict:
    rng = np.random.default_rng(3)
    n_jobs = 20 if QUICK else 60
    wcs = ["WC-CUT", "WC-WELD", "WC-MACH", "WC-PAINT", "WC-INSP"]
    capacity = {"WC-CUT": 2, "WC-WELD": 1, "WC-MACH": 2, "WC-PAINT": 1,
                "WC-INSP": 1}
    jobs = []
    for i in range(n_jobs):
        ops = [(10 * (k + 1), wcs[k], float(rng.integers(20, 140)))
               for k in range(len(wcs))]
        work = sum(m for _, _, m in ops)
        jobs.append(SCH.Job(f"J{i:03d}", ops,
                            due=float(rng.uniform(1.2, 3.0)) * work,
                            released=float(rng.integers(0, 600))))

    cal = SCH.ShiftCalendar()
    inf = SCH.infinite_capacity(jobs)
    rules = SCH.compare_rules(jobs, capacity)
    fifo = SCH.schedule(jobs, capacity, "FIFO")
    with_cal = SCH.schedule(jobs, capacity, "FIFO", cal)

    return {
        "n_jobs": n_jobs, "capacity": capacity,
        "infinite": {k: inf[k] for k in
                     ("makespan", "mean_flow_time", "n_late", "total_tardiness")},
        "rules": rules,
        "finite_vs_infinite_flow": (fifo["mean_flow_time"]
                                    / max(inf["mean_flow_time"], 1e-9)),
        "queue_share_of_flow": fifo["queue_share_of_flow"],
        "calendar": {"makespan_no_calendar": fifo["makespan"],
                     "makespan_with_calendar": with_cal["makespan"],
                     "inflation": with_cal["makespan"] / max(fifo["makespan"], 1e-9)},
        "calendar_example": cal.elapsed_vs_working(14 * 60, 8 * 60),
    }


# ---------------------------------------------------------------------------
# 3. integration
# ---------------------------------------------------------------------------

def stage_integration() -> dict:
    """DATA-1's machine states gate operations; ML-1's alarms create work orders.

    The README's item 5, and both directions are the same architectural point:
    an MES that does not know the equipment state will happily record production
    on a machine that is down, and a predictive-maintenance model whose alarms do
    not become work orders is a dashboard.

    The interfaces are deliberately narrow -- a state provider and an alarm feed --
    because the alternative is importing another project, and a cross-project
    import is the thing that makes two systems impossible to deploy separately.
    """
    db = _fresh("integration.db")
    conn = model.create(db)
    generate.seed_definitions(conn); generate.seed_lots(conn)
    conn.execute("INSERT INTO work_order VALUES ('WO-8001','BRKT-100',5,"
                 "'IN_PROCESS','2026-07-01T06:00:00+00:00')")
    for i in range(3):
        conn.execute("INSERT INTO unit (unit_id, wo_id, serial, lot_qty, status, "
                     "current_seq) VALUES (?,?,?,NULL,'IN_PROCESS',NULL)",
                     (f"WO-8001-U{i}", "WO-8001", f"SN800{i}"))
    conn.commit()

    states = {"WC-CUT": "RUNNING", "WC-WELD": "UNPLANNED_DOWN",
              "WC-MACH": "RUNNING", "WC-PAINT": "PLANNED_STOP",
              "WC-INSP": "RUNNING"}

    def gate(wc: str) -> tuple[bool, str]:
        st = states.get(wc, "UNKNOWN")
        if st in ("UNPLANNED_DOWN", "PLANNED_STOP"):
            return False, f"{wc} is {st}"
        if st == "UNKNOWN":
            # Fail OPEN on an unknown state, and record it. Failing closed on a
            # missing integration halts a plant because a message bus hiccupped,
            # which is how an integration gets switched off permanently.
            return True, f"{wc} state unknown -- allowed, but recorded"
        return True, ""

    attempts, blocked = [], 0
    for wc in ("WC-CUT", "WC-WELD", "WC-PAINT", "WC-GRIND"):
        ok, why = gate(wc)
        attempts.append({"wc": wc, "state": states.get(wc, "UNKNOWN"),
                         "allowed": ok, "reason": why})
        if not ok:
            blocked += 1
            ex.audit(conn, None, wc, "BLOCKED_BY_EQUIPMENT_STATE", wc, why)

    # ML-1 alarms -> maintenance work orders. Deduplicated per asset, because a
    # model that alarms every cycle would otherwise raise a work order every
    # cycle, and maintenance would filter the feed within a week.
    alarms = [{"asset": "WC-WELD", "rul": 18.0, "confidence": 0.82},
              {"asset": "WC-WELD", "rul": 17.0, "confidence": 0.84},
              {"asset": "WC-MACH", "rul": 61.0, "confidence": 0.55}]
    conn.execute("""CREATE TABLE IF NOT EXISTS maintenance_wo (
        mwo_id INTEGER PRIMARY KEY AUTOINCREMENT, asset TEXT NOT NULL,
        source TEXT NOT NULL, predicted_rul REAL, confidence REAL,
        priority TEXT NOT NULL, created_ts TEXT NOT NULL,
        UNIQUE(asset, source))""")
    created, deduped = 0, 0
    for a in alarms:
        pri = "URGENT" if a["rul"] < 24 else "PLANNED"
        try:
            conn.execute(
                "INSERT INTO maintenance_wo (asset, source, predicted_rul, "
                "confidence, priority, created_ts) VALUES (?,?,?,?,?,?)",
                (a["asset"], "ML-1", a["rul"], a["confidence"], pri, ex._now()))
            created += 1
        except sqlite3.IntegrityError:
            deduped += 1
    conn.commit()
    mwos = [dict(r) for r in conn.execute("SELECT * FROM maintenance_wo")]
    conn.close()
    return {"gate_attempts": attempts, "blocked": blocked,
            "maintenance_wos_created": created, "alarms_deduped": deduped,
            "maintenance_wos": mwos,
            "unknown_state_policy": ("fail OPEN and record -- failing closed on a "
                                     "missing integration halts a plant because a "
                                     "message bus hiccupped")}


# ---------------------------------------------------------------------------
# 5. scale
# ---------------------------------------------------------------------------

def stage_scale() -> dict:
    """Grow the genealogy to millions of edges and re-time the recall query.

    The README's item 8: the recall query was measured against 123 edges with
    warm caches, and "both numbers would change shape at millions of rows, where
    the index design starts to matter". Here is the shape.
    """
    db = _fresh("scale.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE consumption (
            cons_id INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_id TEXT NOT NULL, wo_id TEXT NOT NULL, seq INTEGER NOT NULL,
            lot_id TEXT NOT NULL, qty REAL NOT NULL);
        CREATE TABLE lot (
            lot_id TEXT PRIMARY KEY, parent_lot TEXT, part TEXT, qty REAL);
    """)
    n_edges = 200_000 if QUICK else 2_000_000
    n_lots = 20_000
    rng = np.random.default_rng(7)
    batch, target = [], n_edges
    t0 = time.perf_counter()
    for i in range(target):
        batch.append((f"U{i // 5:07d}", f"WO{i // 500:05d}", 10 * (i % 5 + 1),
                      f"L{int(rng.integers(0, n_lots)):06d}", 1.0))
        if len(batch) >= 50_000:
            conn.executemany("INSERT INTO consumption (unit_id, wo_id, seq, "
                             "lot_id, qty) VALUES (?,?,?,?,?)", batch)
            batch.clear()
    if batch:
        conn.executemany("INSERT INTO consumption (unit_id, wo_id, seq, lot_id, "
                         "qty) VALUES (?,?,?,?,?)", batch)
    conn.commit()
    insert_s = time.perf_counter() - t0

    probe = f"L{int(rng.integers(0, n_lots)):06d}"

    def timed(sql, params, n=5):
        best = float("inf")
        for _ in range(n):
            t = time.perf_counter()
            conn.execute(sql, params).fetchall()
            best = min(best, time.perf_counter() - t)
        return best * 1000

    q = "SELECT DISTINCT unit_id FROM consumption WHERE lot_id = ?"
    no_index_ms = timed(q, (probe,))

    t0 = time.perf_counter()
    conn.execute("CREATE INDEX ix_cons_lot ON consumption(lot_id)")
    conn.commit()
    index_build_s = time.perf_counter() - t0
    with_index_ms = timed(q, (probe,))

    # The covering index: lot_id AND unit_id, so the query never touches the
    # table. This is the difference between an index that finds rows and one
    # that ANSWERS the query.
    t0 = time.perf_counter()
    conn.execute("CREATE INDEX ix_cons_lot_unit ON consumption(lot_id, unit_id)")
    conn.commit()
    covering_build_s = time.perf_counter() - t0
    covering_ms = timed(q, (probe,))
    plan = [tuple(r) for r in conn.execute(
        "EXPLAIN QUERY PLAN " + q, (probe,))]

    size_mb = db.stat().st_size / 1e6
    conn.close()
    return {
        "edges": target, "insert_seconds": insert_s,
        "insert_rows_per_second": target / max(insert_s, 1e-9),
        "db_mb": size_mb,
        "recall_ms_no_index": no_index_ms,
        "recall_ms_with_index": with_index_ms,
        "recall_ms_covering_index": covering_ms,
        "speedup_index": no_index_ms / max(with_index_ms, 1e-9),
        "speedup_covering": with_index_ms / max(covering_ms, 1e-9),
        "index_build_seconds": index_build_s,
        "covering_build_seconds": covering_build_s,
        "query_plan": plan,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    OUT.mkdir(exist_ok=True)
    DOCS.mkdir(exist_ok=True)
    if "--report-only" in sys.argv:
        res = json.loads((OUT / "completion.json").read_text(encoding="utf-8"))
        (DOCS / "COMPLETION.md").write_text(report(res), encoding="utf-8")
        print("re-rendered docs/COMPLETION.md")
        return

    t0 = time.perf_counter()
    res: dict = {"quick": QUICK}

    print("1/6 concurrency: eight operators, one operation ...", flush=True)
    res["concurrency"] = stage_concurrency()
    g = res["concurrency"]["guarded"]
    print(f"    guarded: {g['winners']} winner / {g['losers']} refused; "
          f"unguarded: {res['concurrency']['unguarded']['winners']} winners",
          flush=True)

    print("2/6 finite-capacity scheduling ...", flush=True)
    res["scheduling"] = stage_scheduling()
    print(f"    finite/infinite flow time "
          f"{res['scheduling']['finite_vs_infinite_flow']:.2f}x", flush=True)

    print("3/6 integration with DATA-1 states and ML-1 alarms ...", flush=True)
    res["integration"] = stage_integration()

    print("4/6 electronic signatures and retention ...", flush=True)
    res["signature"] = SIG.demo(_fresh("esig.db"))

    print("5/6 scale: millions of genealogy edges ...", flush=True)
    res["scale"] = stage_scale()
    s = res["scale"]
    print(f"    {s['edges']:,} edges: {s['recall_ms_no_index']:.0f} ms -> "
          f"{s['recall_ms_covering_index']:.2f} ms", flush=True)

    print("6/6 operator terminal ...", flush=True)
    res["terminal"] = TERM.render(OUT / "terminal.html", res)

    res["wall_seconds"] = time.perf_counter() - t0
    (OUT / "completion.json").write_text(
        json.dumps(res, indent=1, default=str), encoding="utf-8")
    (DOCS / "COMPLETION.md").write_text(report(res), encoding="utf-8")
    print(f"\nwrote docs/COMPLETION.md and out/terminal.html "
          f"({res['wall_seconds']:.0f}s)")


def report(res: dict) -> str:
    L: list[str] = []
    A = L.append
    cc, sc, ig, sg, sl = (res["concurrency"], res["scheduling"],
                          res["integration"], res["signature"], res["scale"])
    A("# SE-2 completion — generated by `complete.py`, not hand-edited\n")

    A("## 1. Concurrency, and the guard that was not enough\n")
    A("The README refused to call the double-completion guard race-safe: it is a "
      "read-then-write with no transaction boundary, so two threads can both read "
      "\"not yet completed\" before either writes. That was correct, and here is "
      "the race.\n")
    g, u = cc["guarded"], cc["unguarded"]
    A("| | threads | completions accepted | refused | exactly one won |")
    A("|---|---|---|---|---|")
    A(f"| **with BEGIN IMMEDIATE + UNIQUE index** | {g['n_threads']} "
      f"| **{g['winners']}** | {g['losers']} | **{g['exactly_one_won']}** |")
    A(f"| without the index | {u['n_threads']} | {u['winners']} | {u['losers']} "
      f"| {u['exactly_one_won']} |")
    A(f"\nEight threads released simultaneously off a barrier — without the "
      "barrier they run one after another and the test passes for the wrong "
      f"reason. Refusal reasons: `{', '.join(str(x) for x in g['loss_reasons'])}`.\n")
    A(f"{cc['why']}. The index is the half that matters: an application check "
      "protects code that goes through it, and a constraint protects against "
      "everything — including the migration script somebody runs at 2 a.m.\n")
    A("`pass_no` makes the constraint expressible at all. A reworked unit "
      "legitimately completes the same operation twice, so the unique key is "
      "(unit, seq, **pass**) rather than (unit, seq) — which is the same "
      "insight the rework bugs in pass 2 turned on.\n")

    A("## 2. Finite-capacity scheduling\n")
    A(f"`work_center.capacity` has existed since pass 1 and nothing read it. "
      f"With {sc['n_jobs']} jobs across five work centres:\n")
    A("| plan | makespan | mean flow time | jobs late | total tardiness |")
    A("|---|---|---|---|---|")
    i = sc["infinite"]
    A(f"| infinite capacity (not a plan) | {i['makespan']:.0f} "
      f"| {i['mean_flow_time']:.0f} | {i['n_late']} | {i['total_tardiness']:.0f} |")
    for r in sc["rules"]:
        A(f"| finite, {r['rule']} | {r['makespan']:.0f} "
          f"| {r['mean_flow_time']:.0f} | {r['n_late']} "
          f"| {r['total_tardiness']:.0f} |")
    A(f"\n**Finite-capacity flow time is {sc['finite_vs_infinite_flow']:.1f}× the "
      f"infinite-capacity plan**, and "
      f"**{sc['queue_share_of_flow'] * 100:.0f}% of it is queue time**. That is "
      "the number an infinite-capacity planner promises away: a plant's lead time "
      "is dominated by waiting, not by cutting, and a plan that schedules three "
      "jobs onto one machine at 08:00 is not optimistic, it is arithmetically "
      "impossible.\n")
    best_flow = min(sc["rules"], key=lambda r: r["mean_flow_time"])
    best_late = min(sc["rules"], key=lambda r: r["max_lateness"])
    A(f"**No rule dominates, which is the point of comparing them.** "
      f"{best_flow['rule']} gives the best mean flow time "
      f"({best_flow['mean_flow_time']:.0f}) and {best_late['rule']} the best "
      f"worst-case lateness. Choosing between them is choosing what the plant is "
      "judged on — average throughput or the one order that embarrasses you — "
      "and that is a business decision, not a technical one.\n")
    cal = sc["calendar"]
    ce = sc["calendar_example"]
    A(f"**The shift calendar is not decoration.** An "
      f"{ce['working_minutes'] / 60:.0f}-hour job started at 14:00 takes "
      f"{ce['elapsed_minutes'] / 60:.1f} elapsed hours — "
      f"{ce['calendar_inflation']:.2f}× — because it crosses a break and the end "
      f"of second shift. Across the whole schedule the makespan inflates "
      f"{cal['inflation']:.2f}×.\n")

    A("## 3. Integration with DATA-1 and ML-1\n")
    A(f"**Equipment state gates operations:** {ig['blocked']} of "
      f"{len(ig['gate_attempts'])} attempted starts refused.\n")
    A("| work centre | state | allowed | reason |")
    A("|---|---|---|---|")
    for a in ig["gate_attempts"]:
        A(f"| {a['wc']} | {a['state']} | {'yes' if a['allowed'] else '**no**'} "
          f"| {a['reason'] or '—'} |")
    A(f"\n**An unknown state fails OPEN and is recorded.** "
      f"{ig['unknown_state_policy']}. That is a deliberate choice with a cost — a "
      "genuinely down machine whose state never arrived will accept production — "
      "and the audit row is what makes the cost visible afterwards.\n")
    A(f"**ML-1 alarms become maintenance work orders:** "
      f"{ig['maintenance_wos_created']} created, {ig['alarms_deduped']} "
      "suppressed as duplicates. A model that alarms every cycle would otherwise "
      "raise a work order every cycle, and maintenance would filter the feed "
      "within a week.\n")
    A("Both interfaces are narrow on purpose — a state provider and an alarm "
      "feed — because importing another project is what makes two systems "
      "impossible to deploy separately.\n")

    A("## 4. Electronic signatures and retention\n")
    A(f"{sg['summary']}\n")
    A("| attempted violation | refused |")
    A("|---|---|")
    for c in sg["checks"]:
        A(f"| {c['attempt']} | {'**yes**' if c['refused'] else 'NO'} |")
    A(f"\n**The audit chain is hash-linked**: each row carries the hash of its "
      f"predecessor, so deleting or editing a row breaks every hash after it. "
      f"Tamper detection: {sg['tamper_detected']}, at row "
      f"{sg.get('tamper_row')}.\n")
    A("This is 21 CFR Part 11 *shaped* — signature meaning, signer identity, "
      "timestamp, and a link to the signed record — and it is **not** a compliant "
      "system. No identity provider, no password policy, no periodic review, no "
      "validation package. The gap is smaller than it was and it is still a gap.\n")

    A("## 5. Scale\n")
    A(f"{sl['edges']:,} genealogy edges, {sl['db_mb']:.0f} MB, inserted at "
      f"{sl['insert_rows_per_second']:,.0f} rows/s.\n")
    A("| recall query | time |")
    A("|---|---|")
    A(f"| no index (full scan) | **{sl['recall_ms_no_index']:.1f} ms** |")
    A(f"| index on lot_id | {sl['recall_ms_with_index']:.2f} ms "
      f"({sl['speedup_index']:.0f}× faster) |")
    A(f"| **covering index (lot_id, unit_id)** "
      f"| **{sl['recall_ms_covering_index']:.2f} ms** "
      f"({sl['speedup_covering']:.1f}× faster again) |")
    A(f"\nThe README said the 0.7 ms figure was measured against 123 edges with "
      f"warm caches and would change shape at millions of rows. It does: "
      f"unindexed, the same query takes {sl['recall_ms_no_index']:.0f} ms.\n")
    A("The second step is the one worth knowing. A plain index on `lot_id` finds "
      "the rows and then reads each one from the table; a **covering** index on "
      "`(lot_id, unit_id)` contains the answer, so the query never touches the "
      f"table at all. Query plan: `{sl['query_plan']}`.\n")

    t = res["terminal"]
    A("## 6. The operator terminal\n")
    A(f"`out/terminal.html`, {t['bytes'] / 1024:.0f} KB, self-contained. The "
      "dispatch list, the unit's routing with its completed operations, the "
      "materials to issue, and the disposition buttons. It renders the state and "
      "does not write — wiring it to `execution.py` needs a server, and a UI that "
      "silently no-ops would be worse than none.\n")

    A("---")
    A(f"*Generated in {res.get('wall_seconds', 0):.0f}s"
      f"{' (quick mode)' if res.get('quick') else ''}.*")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
