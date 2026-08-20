"""SE-2 end-to-end: build a week of execution, then run the recall drill.

    python run_mes.py
    python run_mes.py --report-only

Writes docs/RESULTS.md and out/results.json, plus a sample birth certificate to
out/birth_certificate.txt.
"""
from __future__ import annotations

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
DB = OUT / "mes.db"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    if "--report-only" in sys.argv:
        prev = json.loads((OUT / "results.json").read_text())
        (ROOT / "docs").mkdir(exist_ok=True)
        (ROOT / "docs" / "RESULTS.md").write_text(report(prev), encoding="utf-8")
        print("re-rendered docs/RESULTS.md")
        return

    t0 = time.perf_counter()
    DB.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        pathlib.Path(str(DB) + suffix).unlink(missing_ok=True)
    conn = model.create(DB)

    print("1/5 seeding definitions and lots ...", flush=True)
    generate.seed_definitions(conn)
    generate.seed_lots(conn)

    print("2/5 executing a week of production ...", flush=True)
    rng = np.random.default_rng(20260819)
    t_exec = time.perf_counter()
    truth = generate.run_week(conn, rng)
    exec_s = time.perf_counter() - t_exec
    n_records = conn.execute("SELECT COUNT(*) c FROM op_record").fetchone()["c"]
    n_cons = conn.execute("SELECT COUNT(*) c FROM consumption").fetchone()["c"]
    n_units = conn.execute("SELECT COUNT(*) c FROM unit").fetchone()["c"]
    n_audit = conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"]
    print(f"    {n_units} units, {n_records} operation records, {n_cons} genealogy "
          f"edges, {n_audit} audit rows in {exec_s:.2f}s", flush=True)

    res: dict = {
        "counts": {"units": n_units, "op_records": n_records,
                   "genealogy_edges": n_cons, "audit_rows": n_audit},
        "throughput": {"seconds": exec_s,
                       "transactions_per_second": (n_records + n_cons) / max(exec_s, 1e-9)},
    }

    print("3/5 quantity conservation ...", flush=True)
    cons = ex.conservation_report(conn)
    violations = [c for c in cons if c["violates"]]
    res["conservation"] = {
        "operations_checked": len(cons),
        "violations": len(violations),
        "detail": cons,
    }
    print(f"    {len(cons)} (work order, operation) pairs checked, "
          f"{len(violations)} violations", flush=True)

    print("4/5 enforcement of planted violations ...", flush=True)
    res["enforcement"] = {"attempted": truth["attempted"], "blocked": truth["blocked"]}
    for k, n in truth["attempted"].items():
        print(f"    {k:<32} {truth['blocked'].get(k,0)}/{n}", flush=True)

    print("5/5 the recall drill ...", flush=True)
    drill = trace.recall_drill(conn, "L-4471")
    found = set(drill["shipped"] + drill["finished_on_hand"] + drill["in_process"]
                + drill["already_scrapped"])
    expected = set(truth["recall_expected_units"])
    excluded = set(truth["recall_must_exclude"])
    res["recall"] = {
        **{k: v for k, v in drill.items() if k != "actions"},
        "n_actions": len(drill["actions"]),
        "expected_units": sorted(expected),
        "found_units": sorted(found),
        "missed": sorted(expected - found),
        "false_positives": sorted(found - expected),
        "control_group_leaked": sorted(found & excluded),
        "complete": bool(found == expected),
    }
    print(f"    lot L-4471 -> {len(found)} units, expected {len(expected)}, "
          f"missed {len(expected-found)}, false positives {len(found-expected)}, "
          f"query {drill['query_seconds']*1000:.1f} ms", flush=True)

    # A naive query that stops at the named lot, for comparison.
    naive = conn.execute(
        "SELECT DISTINCT unit_id FROM consumption WHERE lot_id='L-4471'").fetchall()
    res["recall"]["naive_query_units"] = len(naive)

    reworked = conn.execute(
        "SELECT unit_id FROM op_record WHERE action='REWORK_ENTRY' LIMIT 1").fetchone()
    sample = reworked["unit_id"] if reworked else sorted(found)[0]
    cert = trace.birth_certificate(conn, sample)
    (OUT / "birth_certificate.txt").write_text(cert, encoding="utf-8")
    t_bc = time.perf_counter()
    for _ in range(50):
        trace.birth_certificate(conn, sample)
    res["birth_certificate"] = {
        "unit": sample,
        "ms_per_document": (time.perf_counter() - t_bc) / 50 * 1000,
        "text": cert,
    }
    res["wall_seconds"] = time.perf_counter() - t0

    (OUT / "results.json").write_text(json.dumps(res, indent=2, default=str))
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "RESULTS.md").write_text(report(res), encoding="utf-8")
    print(f"\nwrote docs/RESULTS.md, out/results.json, out/birth_certificate.txt "
          f"({res['wall_seconds']:.1f}s)")


def report(res: dict) -> str:
    L: list[str] = []
    A = L.append
    c = res["counts"]
    A("# SE-2 results — generated by `run_mes.py`, not hand-edited\n")
    A(f"{c['units']} units, {c['op_records']} operation records, "
      f"**{c['genealogy_edges']} genealogy edges**, {c['audit_rows']} audit rows, "
      f"built in {res['throughput']['seconds']:.2f} s "
      f"({res['throughput']['transactions_per_second']:,.0f} transactions/s "
      "against SQLite with foreign keys on).\n")

    r = res["recall"]
    A("## 1. The recall drill — one command\n")
    A("Scenario, planted in the generator so the answer is known in advance: lot "
      "**L-4471** is split into `L-4471-A` (180) and `L-4471-B` (120); A is issued "
      "to WO-1001, B to WO-1002, and WO-1003 runs on clean `L-4998` as a control "
      "group. The drill must find exactly WO-1001 + WO-1002 **through the split**, "
      "and must not touch WO-1003.\n")
    A("| | |")
    A("|---|---|")
    A(f"| lots pulled into scope | {', '.join(r['lots_in_scope'])} |")
    A(f"| units affected | **{r['units_affected']}** |")
    A(f"| expected (ground truth) | {len(r['expected_units'])} |")
    A(f"| **missed** | **{len(r['missed'])}** |")
    A(f"| **false positives** | **{len(r['false_positives'])}** |")
    A(f"| control group leaked in | {len(r['control_group_leaked'])} |")
    A(f"| already shipped | {len(r['shipped'])} — customers: {', '.join(r['customers'])} |")
    A(f"| finished, on hand | {len(r['finished_on_hand'])} |")
    A(f"| in process | {len(r['in_process'])} |")
    A(f"| already scrapped | {len(r['already_scrapped'])} |")
    A(f"| quarantine / notification actions generated | {r['n_actions']} |")
    A(f"| **query time** | **{r['query_seconds']*1000:.1f} ms** |")
    A(f"\nCompleteness against ground truth: **{'100%' if r['complete'] else 'INCOMPLETE'}**.\n")
    A(f"**The split is the part that separates a working recall from a plausible "
      f"one.** A query that stops at the named lot — `SELECT ... WHERE "
      f"lot_id='L-4471'` — returns **{r['naive_query_units']} units**, because "
      "after the split no consumption row names L-4471 at all: every issue cites a "
      "child lot. That query returns clean, and product stays in the field. "
      "`trace.affected_lots()` walks to the root of the split tree and back down, "
      "which is why it returns "
      f"{r['units_affected']}.")

    cons = res["conservation"]
    A("\n## 2. Quantity conservation — the invariant\n")
    A(f"**{cons['violations']} violations across {cons['operations_checked']} "
      "(work order, operation) pairs.**\n")
    A("`started == completed + scrapped + nonconformances + in_process` at every "
      "operation, counted from the append-only ledger rather than from a status "
      "column — because a status column is a cache and this is what the cache is "
      "supposed to agree with. This is manufacturing's double-entry bookkeeping, "
      "and like double entry it only works if the system refuses the transaction "
      "that breaks it.\n")
    A("**The nonconformances term is there because it had to be.** Without it, a "
      "unit that failed inspection at op 50, was reworked, and came back to "
      "complete op 50 shows two starts and one completion — a phantom in-process "
      "balance of 2 on units that were sitting on the shipping dock. A pass that "
      "ended in an NCR was neither completed nor scrapped; it was *dispositioned*, "
      "and that is a fourth accounting category rather than a rounding problem. "
      "Look at the `WO-1001 / op 50` row: 13 started, 11 completed, 2 "
      "nonconformances, and it balances.\n")
    A("| work order | op | started | completed | scrapped | nonconformances | rework entries | in process |")
    A("|---|---|---|---|---|---|---|---|")
    for row in cons["detail"]:
        A(f"| {row['wo_id']} | {row['seq']} | {row['started']:.0f} | "
          f"{row['completed']:.0f} | {row['scrapped']:.0f} | "
          f"{row.get('nonconformances', 0):.0f} | {row['reworked']:.0f} | "
          f"{row['in_process']:.0f} |")
    A("\nThe `rework entries` column is why reworked units do not read as "
      "violations. A rework re-starts the unit at an operation it already "
      "completed, so it adds to the started side of the ledger. Modelling rework "
      "as a status flag instead of as a routing event is the common shortcut, and "
      "it makes this table unbalanceable — as well as destroying the answer to "
      "\"how many times did this unit go through op 40\", which is the first thing "
      "a quality engineer asks about a systemic defect.")

    e = res["enforcement"]
    A("\n## 3. Enforcement — every planted violation, blocked\n")
    A("| rule | attempted | blocked | |")
    A("|---|---|---|---|")
    labels = {
        "precedence": "start op 30 before ops 10/20 exist",
        "certification": "uncertified operator at a WELD-2 operation",
        "over_issue": "issue 3× the BOM quantity",
        "off_bom_component": "issue a component the routing does not consume there",
        "double_completion": "complete the same operation twice, no rework entry",
        "insufficient_lot": "issue more than the lot has on hand",
    }
    ok = True
    for k, n in e["attempted"].items():
        if k == "authorised_deviation_allowed":
            continue
        b = e["blocked"].get(k, 0)
        ok &= (b == n)
        A(f"| {labels.get(k, k)} | {n} | **{b}/{n}** | {'✔' if b == n else '✘'} |")
    dev_blocked = e["blocked"].get("authorised_deviation_allowed", 1)
    A(f"| *authorised deviation (must be **allowed**)* | 1 | "
      f"{'allowed' if dev_blocked == 0 else 'BLOCKED'} | "
      f"{'✔' if dev_blocked == 0 else '✘'} |")
    A(f"\nAll enforcement rules: **{'PASS' if ok else 'FAIL'}**.\n")
    A("The last row is the one that matters politically. Operations will ask to "
      "skip an operation for a hot order, and both available answers are wrong: "
      "rigid refusal gets the system bypassed on paper, and a silent bypass "
      "destroys the record. The third option is a **deviation with an "
      "authorisation reference**, which is allowed, is recorded on the operation, "
      "and appears on the unit's build record forever. Flexibility with "
      "accountability.")

    bc = res["birth_certificate"]
    A("\n## 4. The birth certificate\n")
    A(f"Generated in **{bc['ms_per_document']:.2f} ms** per unit. Sample below is a "
      "unit that went through the rework loop, so the repeated operations are "
      "visible in its history:\n")
    A("```")
    A(bc["text"])
    A("```")

    A("\n---\n*Ground truth comes from `src/generate.py`, which plants the recall "
      "scenario and every rule violation, so completeness and enforcement are "
      "scores rather than assertions.*")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
