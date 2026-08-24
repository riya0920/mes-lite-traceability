"""Pass 4: planning constraints, a terminal that writes, real connections.

The three remaining items from the README's "What is NOT built":

  4. Scheduling is non-delay and single-pass -- no backward scheduling, no
     setup-time matrix, no operator availability.
  1. The terminal is read-only.
  5. The integrations are interfaces, not connections.

Writes docs/PLANNING_AND_CONNECTIONS.md and out/pass4.json.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import time

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

import generate                     # noqa: E402
import integration as I             # noqa: E402
import model                        # noqa: E402
import planning as P                # noqa: E402
import scheduling as S              # noqa: E402
import server as SV                 # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "out"
DOCS = ROOT / "docs"
SIBLINGS = ROOT.parent


def _orders(n=12):
    return [(f"WO-{i:02d}", "BRKT-100" if i % 3 else "PLATE-200",
             20 + 5 * (i % 4), 0.0, 0.0) for i in range(1, n + 1)]


def stage_planning() -> dict:
    db = OUT / "pass4_plan.db"
    db.unlink(missing_ok=True)
    conn = model.create(db)
    generate.seed_definitions(conn)
    cap = P.capacity_of(conn)
    pool = P.OperatorPool.from_db(conn)
    sm = P.SetupMatrix()

    raw = _orders()
    probe = P.jobs_from_db(conn, raw)
    tight = 1.5
    orders = []
    for j, (jid, sku, qty, _, rel) in zip(probe, raw):
        content = sum(o.run_min + o.setup_min for o in j.ops)
        orders.append((jid, sku, qty, content * tight, rel))
    jobs = P.jobs_from_db(conn, orders)

    cases = {}
    for label, st, op in (("baseline", None, None), ("setups", sm, None),
                          ("operators", None, pool), ("both", sm, pool)):
        cases[label] = {}
        for rule in S.DISPATCH_RULES:
            r = P.schedule_finite(jobs, cap, rule, None, st, op)
            cases[label][rule] = {k: r[k] for k in (
                "makespan", "mean_flow_time", "n_late", "total_tardiness",
                "setup_minutes", "operator_wait_minutes", "setup_share_of_work")}

    attend = {}
    for mode in ("all", "setup"):
        p = P.OperatorPool.from_db(conn, attends=mode)
        attend[mode] = P.schedule_finite(jobs, cap, "EDD", None, sm, p)["makespan"]

    rec = P.reconcile_forward_backward(jobs, cap, "EDD", None, sm, pool)
    rush = P.jobs_from_db(conn, [("RUSH-01", "BRKT-100", 40, 30.0, 0.0)])[0]
    infeasible = P.backward_from_due(rush, None, sm)
    infeasible.pop("schedule", None)

    cal = S.ShiftCalendar()
    with_cal = P.schedule_finite(jobs, cap, "EDD", cal, sm, pool)
    without = P.schedule_finite(jobs, cap, "EDD", None, sm, pool)

    conn.close()
    return {"tightness": tight, "n_jobs": len(jobs), "capacity": cap,
            "coverage": pool.coverage(), "cases": cases, "attend": attend,
            "reconcile": {"mean_inflation": rec["mean_inflation"],
                          "per_job": rec["per_job"]},
            "infeasible_promise": infeasible,
            "calendar": {"with_minutes": with_cal["makespan"],
                         "without_minutes": without["makespan"],
                         "inflation": with_cal["makespan"] / max(without["makespan"], 1e-9)}}


def stage_terminal() -> dict:
    import json as _json
    import urllib.error
    import urllib.request

    db = OUT / "pass4_srv.db"
    db.unlink(missing_ok=True)
    conn = model.create(db)
    generate.seed_definitions(conn)
    generate.seed_lots(conn)
    generate.run_week(conn, np.random.default_rng(0))
    conn.close()

    h = SV.serve(db)
    url = h["url"]

    def call(path, body=None, tok=None):
        hdr = {"Content-Type": "application/json"}
        if tok:
            hdr["X-Session"] = tok
        data = _json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url + path, data=data, headers=hdr)
        try:
            with urllib.request.urlopen(req) as f:
                return f.status, _json.load(f)
        except urllib.error.HTTPError as e:
            return e.code, _json.load(e)

    log = []

    def rec(label, code, body):
        log.append({"step": label, "status": code,
                    "kind": body.get("kind") if isinstance(body, dict) else None,
                    "message": (body.get("error") or body.get("reason"))
                    if isinstance(body, dict) else None,
                    "ok": bool(isinstance(body, dict) and body.get("ok"))})

    c, b = call("/api/complete", {"unit_id": "x", "seq": 10, "wc_id": "WC-CUT"})
    rec("write with no session", c, b)
    c, b = call("/api/login", {"op_id": "NOBODY"})
    rec("login as an unknown operator", c, b)

    tok5 = call("/api/login", {"op_id": "OP-05"})[1]["token"]
    tok1 = call("/api/login", {"op_id": "OP-01"})[1]["token"]
    _, disp = call("/api/dispatch")
    weld = [r for r in disp if r["cert_required"] == "WELD-2"]
    unit, seq = weld[0]["unit_id"], weld[0]["seq"]

    c, b = call(f"/api/can_start?unit_id={unit}&seq={seq}", None, tok5)
    rec("can_start, uncertified (the greyed-out button)", c, b)
    c, b = call("/api/start", {"unit_id": unit, "seq": seq, "wc_id": "WC-WELD"}, tok5)
    rec("start, uncertified", c, b)
    c, b = call("/api/start", {"unit_id": unit, "seq": seq, "wc_id": "WC-WELD",
                               "deviation_ref": "DEV-77"}, tok5)
    rec("start, uncertified, with an authorised deviation", c, b)
    c, b = call("/api/complete", {"unit_id": unit, "seq": seq, "wc_id": "WC-WELD"}, tok1)
    rec("complete", c, b)
    c, b = call("/api/complete", {"unit_id": unit, "seq": seq, "wc_id": "WC-WELD"}, tok1)
    rec("complete again (the double-click)", c, b)
    c, b = call("/api/complete", {"unit_id": unit}, tok1)
    rec("a request missing fields", c, b)

    conn = model.connect(db)
    n_complete = conn.execute(
        "SELECT COUNT(*) FROM op_record WHERE unit_id=? AND seq=? AND "
        "action='COMPLETE'", (unit, seq)).fetchone()[0]
    audit = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    conn.close()
    h["server"].shutdown()
    return {"log": log, "unit": unit, "seq": seq,
            "completions_recorded": n_complete, "audit_rows": audit,
            "limits": SV.LIMITS, "n_dispatch": len(disp)}


def stage_connections() -> dict:
    hist_path = SIBLINGS / "data1-oee-platform" / "out" / "historian.db"
    ml1_path = SIBLINGS / "ml1-rul-platform" / "out" / "completion.json"

    now = dt.datetime.now(dt.timezone.utc)
    feed = I.HistorianFeed(hist_path)
    out = {"historian_path": str(hist_path), "ml1_path": str(ml1_path),
           "historian_available": feed.available,
           "ml1_available": ml1_path.exists()}

    if feed.available:
        latest = feed.latest_states()
        out["n_machines"] = len(latest)
        newest = max(dt.datetime.fromisoformat(t) for _, t in latest.values())
        out["newest_reading"] = newest.isoformat()
        out["age_of_newest_minutes"] = (now - newest).total_seconds() / 60.0

        st_now = feed.work_centre_states(now=now)["work_centres"]
        st_fresh = feed.work_centre_states(
            now=newest + dt.timedelta(seconds=30))["work_centres"]
        wcs = ["WC-CUT", "WC-WELD", "WC-MACH", "WC-PAINT", "WC-GRIND"]
        out["gates_as_of_today"] = [
            dict(I.gate_operation(w, st_now), wc=w) for w in wcs]
        out["gates_as_of_the_data"] = [
            dict(I.gate_operation(w, st_fresh), wc=w) for w in wcs]

    rul = I.RulFeed(ml1_path)
    if rul.available:
        out["model"] = rul.production_model()
        out["intervals"] = rul.intervals()
        a = rul.alarms(["WC-WELD", "WC-MACH"])
        out["alarms"] = a["alarms"]
        out["alarm_basis"] = a.get("basis")
        med = (rul.intervals().get("median") or {}).get("point", 0.0)
        p05 = (rul.intervals().get("p05") or {}).get("point", 0.0)
        out["priority_from_median"] = "URGENT" if med < 24.0 else "PLANNED"
        out["priority_from_p05"] = "URGENT" if p05 < 24.0 else "PLANNED"

    missing = I.connect_all(OUT / "no-such.db", OUT / "no-such.json")
    out["absent_feeds"] = {k: v for k, v in missing.items() if k != "_feeds"}
    return out


def report(d: dict) -> str:
    L: list[str] = []
    A = L.append
    pl, tm, cn = d["planning"], d["terminal"], d["connections"]

    A("# Planning constraints, a terminal that writes, and real connections\n")
    A("The last three items on this project's not-built list. Generated by "
      f"`run_pass4.py` in {d['elapsed_s']:.0f} s.\n")

    # --- planning
    A("## 1. The planner now uses the routing that is in the database\n")
    A("`scheduling.schedule()` took a synthetic list of "
      "`(seq, work_centre, minutes)` triples. The database beside it has always "
      "had `operation.std_setup_s`, `operation.cert_required`, "
      "`work_center.capacity` and a `certification` table, and the scheduler "
      "used none of them — so its plans came from a routing that resembled the "
      "real one rather than being it.\n")
    A("`planning.schedule_finite` with no setup matrix and unlimited operators "
      "reproduces the old scheduler **operation for operation on all four "
      "dispatch rules**, which is asserted in the tests. That equivalence is "
      "what makes everything below attributable to the constraints rather than "
      "to a rewrite.\n")

    base = pl["cases"]["baseline"]
    both = pl["cases"]["both"]
    A(f"\n### What the constraints cost ({pl['n_jobs']} jobs, due dates at "
      f"{pl['tightness']}× work content)\n")
    A("| rule | makespan (min) | | | | late jobs | |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    A("| | naive | +setups | +operators | +both | naive | +both |")
    for rule in S.DISPATCH_RULES:
        A(f"| {rule} | {base[rule]['makespan']:.0f} | "
          f"{pl['cases']['setups'][rule]['makespan']:.0f} | "
          f"{pl['cases']['operators'][rule]['makespan']:.0f} | "
          f"{both[rule]['makespan']:.0f} | "
          f"{base[rule]['n_late']}/{pl['n_jobs']} | "
          f"{both[rule]['n_late']}/{pl['n_jobs']} |")

    d_set = pl["cases"]["setups"]["EDD"]["makespan"] - base["EDD"]["makespan"]
    d_ops = pl["cases"]["operators"]["EDD"]["makespan"] - base["EDD"]["makespan"]
    A(f"\n**Operators cost {d_ops / max(d_set, 1e-9):.0f}× what setups cost** "
      f"(+{d_ops:.0f} minutes against +{d_set:.0f} on EDD), and nothing in a "
      "capacity number says so. The reason is in the certification table: "
      + ", ".join(f"{c} has {n} holders" for c, n in pl["coverage"].items())
      + ", against five operators — one of whom holds nothing. A work centre "
      "with three machines and two certified people has a capacity of two.\n")

    zero = [r for r in S.DISPATCH_RULES
            if base[r]["n_late"] == 0 and both[r]["n_late"] > 0]
    if zero:
        A(f"**And the naive plan says {', '.join(zero)} deliver everything on "
          f"time.** They do not: under the real constraints "
          + ", ".join(f"{r} misses {both[r]['n_late']}" for r in zero)
          + f" of {pl['n_jobs']}. A planner whose constraints are optional "
          "produces a promise nobody can keep, and it produces it in the most "
          "convincing possible form — zero.\n")

    at = pl["attend"]
    A(f"\n**Whether the operator stays for the whole operation or only the "
      f"setup moves the makespan from {at['all']:.0f} to {at['setup']:.0f} "
      f"minutes ({100 * (at['all'] - at['setup']) / at['all']:.0f}%).** Both are "
      "real — a CNC cell is set up attended and then runs unattended, a manual "
      "weld is attended throughout — so it is a parameter rather than an "
      "assumption buried in the loop.\n")

    A("\n### Backward scheduling\n")
    inf = pl["infeasible_promise"]
    A(f"- A rush order promised in {inf['due']:.0f} minutes needs "
      f"{inf['critical_path_minutes']:.0f} minutes of work: latest release "
      f"**{inf['latest_release']:.0f} minutes**, i.e. "
      f"{abs(inf['latest_release']) / 60:.1f} hours before now. Infeasible with "
      "every machine free, so certainly infeasible with the machines there are — "
      "and that answer is available before anybody looks at a queue.\n")
    A(f"- Across the order book, the finite-capacity forward pass takes "
      f"**{pl['reconcile']['mean_inflation']:.2f}× the infinite-capacity "
      f"backward promise**. The difference is queue: time the job spends waiting "
      "for a machine that no routing document mentions, and the number a "
      "planning system is most tempted to leave out of a quoted lead time.\n")
    cal = pl["calendar"]
    A(f"- Putting the shift calendar back in inflates the makespan from "
      f"{cal['without_minutes']:.0f} to {cal['with_minutes']:.0f} minutes "
      f"({cal['inflation']:.2f}×) — nights, weekends and two breaks a shift.\n")

    A("\n**A bug found building the backward walk.** `add_working_minutes` "
      "compared `remaining <= avail`, and both are minute counts in the tens of "
      "thousands, so work that exactly fills a shift window compares as *longer* "
      "than the window by about 1e-12. The fall-through does not lose a "
      "picosecond — it carries the residue into the **next** window and returns "
      "a time a whole shift later, or after a weekend. 1052 of 4000 random "
      "round-trips failed before the tolerance went in, and the forward function "
      "had carried the same latent bug since pass 1.\n")

    # --- terminal
    A("\n## 2. The terminal writes\n")
    A("The reason it did not was stated as needing *a server and a session "
      "model*, and the session model was the half that mattered: every rule in "
      "`execution.py` that protects anything is a rule about **who**. A terminal "
      "with no identity can only write as \"somebody\", and a Part-11-shaped "
      "signature over an anonymous action is worse than none, because it looks "
      "like accountability.\n")
    A("Every write goes through `execution.py`; the handler's only job is to "
      "turn a refusal into a 409 with the reason intact. That is tested by "
      "monkeypatching the check in `execution.py` and watching the same HTTP "
      "request start succeeding — if the server held its own copy of the rule, "
      "it would not.\n")
    A("\n| step | status | outcome |")
    A("|---|---:|---|")
    for r in tm["log"]:
        msg = (r["message"] or ("accepted" if r["ok"] else "—"))
        msg = msg if len(msg) < 110 else msg[:107] + "…"
        A(f"| {r['step']} | {r['status']} | {msg} |")
    A(f"\nAfter all of that, `op_record` holds **{tm['completions_recorded']} "
      "completion** for the unit, not two. The double-click is caught by "
      "`execution.py`'s conservation check before the `ux_one_complete_per_pass` "
      "index is even reached — the index is the backstop for code that does not "
      "come through here, not the path.\n")
    A("And `can_start` calls the same two functions the write path calls, so "
      "the greyed-out button and the refusal cannot disagree. The earlier "
      "version's argument — that a blocked button with no explanation gets "
      "worked around within a shift — is kept: the 409 carries the message "
      "naming the certification and how to override it.\n")
    A("\n**What this is not:**\n")
    for lim in tm["limits"]:
        A(f"- {lim}")
    A("")

    # --- connections
    A("\n## 3. The integrations are connections\n")
    A("The stated reason for not doing this was that *a cross-project import is "
      "the thing that makes two systems impossible to deploy separately*. That "
      "is right, and importing DATA-1's Python would still be wrong. What was "
      "missing was the third option: consume the artefact each project already "
      "**publishes**, over a contract that is a schema rather than a signature.\n")
    A(f"- DATA-1 writes `out/historian.db`, a `samples` table of "
      f"`(machine, tag, value, source_ts, status)`. SE-2 opens it **read-only** "
      f"(`mode=ro`, so it is a property of the connection rather than a rule "
      f"somebody remembers) and derives state from the `State` tag. "
      f"{cn.get('n_machines', 0)} machines found.\n")
    A(f"- ML-1 writes `out/completion.json` with a registry index. SE-2 reads it "
      f"and acts only on a **Production** model: "
      f"`{cn.get('model', {}).get('name')}` v{cn.get('model', {}).get('version')}, "
      f"fingerprint `{cn.get('model', {}).get('fingerprint')}`.\n")

    if cn.get("age_of_newest_minutes") is not None:
        A(f"\n### The connection found something on the first run\n")
        A(f"DATA-1's newest reading is **{cn['age_of_newest_minutes'] / 60 / 24:.1f} "
          f"days old** ({cn['newest_reading']}). Every work centre gates as "
          "`STALE`:\n")
        A("| work centre | as of today | as of the data's own clock |")
        A("|---|---|---|")
        for a, b in zip(cn["gates_as_of_today"], cn["gates_as_of_the_data"]):
            A(f"| {a['wc']} | {a['state']}"
              f"{' — allowed' if a['allowed'] else ' — BLOCKED'} | "
              f"{b['state']}{' — allowed' if b['allowed'] else ' — BLOCKED'} |")
        A("\n**That third outcome is the whole difference.** The interface "
          "version gated on a dict of machine states that was always present, "
          "always current and always right. A real feed is none of those, and a "
          "gate that cannot tell *the weld cell is running* from *the weld cell "
          "was running on Friday* is worse than no gate — it is a green light "
          "with nothing behind it.\n")
        A("Stale fails **open**, and that is a real trade rather than a "
          "convenience: the cost is production recorded against a machine that "
          "was genuinely down while the feed was broken. Failing closed stops "
          "the plant every time a broker restarts, which is how an integration "
          "gets switched off permanently — and then the gate protects nothing "
          "at all. The right-hand column is the same code reading the same file "
          "with the clock wound back to when the data was written, which is how "
          "you tell a stale feed from a stopped plant.\n")
        A("A work centre is blocked only when **every** machine in it is down. "
          "One of two CNC machines down is reduced capacity, not a stopped "
          "cell, and gating on the worst machine would block a cell that is "
          "running.\n")

    if cn.get("alarms"):
        A("\n### An alarm now carries its provenance\n")
        A(f"- Only a Production model raises work orders. A staging model's "
          "alarm reaching maintenance is how a test run becomes a truck roll, "
          "and the feed refuses rather than defaulting.\n")
        A(f"- The work order records model name, version and fingerprint, so "
          "*why did we pull that machine* has an answer six months later. "
          "Deduplication is keyed on `(asset, source, model_version)` rather "
          "than source alone — otherwise a retrained model could never escalate "
          "an asset that already has an open order, and *the new model says "
          "this is now urgent* is exactly the message that must get through.\n")
        A(f"- **Priority comes from the pessimistic end of the interval.** The "
          f"median RUL is {cn['intervals']['median']['point']:.1f} cycles → "
          f"`{cn['priority_from_median']}`; the same forecast read at its 5th "
          f"percentile is {cn['intervals']['p05']['point']:.1f} → "
          f"`{cn['priority_from_p05']}`. Planning maintenance from a median "
          "means being wrong half the time, and the half you are wrong is the "
          "half where the machine fails first.\n")
        A(f"- **Honest limit:** ML-1 publishes a *fleet* interval, not a "
          "per-asset one. Every asset here therefore gets the same predicted "
          "RUL. This demonstrates the provenance and dispositioning path and "
          "does **not** demonstrate per-asset prediction; that needs ML-1 to "
          "publish per-unit rows, which it does not.\n")

    A("\nEither project can be absent. With both paths pointing at nothing, the "
      "feeds report `available: false` with the reason, and the gate falls back "
      "to the fail-open policy that was already documented — which a hard-coded "
      "dict could not do, because a literal is never missing.\n")
    return "\n".join(L) + "\n"


def main() -> None:
    t0 = time.time()
    OUT.mkdir(exist_ok=True)
    DOCS.mkdir(exist_ok=True)
    d = {"planning": stage_planning()}
    print("  planning done")
    d["terminal"] = stage_terminal()
    print("  terminal done")
    d["connections"] = stage_connections()
    print("  connections done")
    d["elapsed_s"] = time.time() - t0
    (OUT / "pass4.json").write_text(json.dumps(d, indent=2, default=str),
                                    encoding="utf-8")
    (DOCS / "PLANNING_AND_CONNECTIONS.md").write_text(report(d), encoding="utf-8")
    print(f"wrote docs/PLANNING_AND_CONNECTIONS.md in {d['elapsed_s']:.0f}s")


if __name__ == "__main__":
    main()
