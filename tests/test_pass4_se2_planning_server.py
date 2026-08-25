"""Pass 4: planning constraints, a terminal that writes, and real connections.

The tests that earn their place here are the ones that pin down a claim the
README now makes: that the new planner is the old one plus constraints (not a
rewrite), that the server holds no rules of its own, and that the feeds behave
when the other project is absent or stale.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import sys
import urllib.error
import urllib.request

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import execution as ex          # noqa: E402
import generate                 # noqa: E402
import integration as I         # noqa: E402
import model                    # noqa: E402
import planning as P            # noqa: E402
import scheduling as S          # noqa: E402
import server as SV             # noqa: E402


# ---------------------------------------------------------------------------
# the shift calendar, backwards
# ---------------------------------------------------------------------------

def test_subtract_is_the_exact_inverse_of_add():
    """Caught a real bug: `remaining <= avail` fails by ~1e-12 when work exactly
    fills a window, and the fall-through does not lose a picosecond -- it places
    the residue in the NEXT window, moving the answer by the gap between shifts,
    or by a whole weekend. 1052 of 4000 random cases before the tolerance."""
    cal = S.ShiftCalendar()
    rng = np.random.default_rng(0)
    for _ in range(3000):
        start = cal.next_working_minute(float(rng.uniform(0, 30 * 1440)))
        mins = float(rng.uniform(0.5, 3000))
        assert cal.subtract_working_minutes(
            cal.add_working_minutes(start, mins), mins) == pytest.approx(
                start, abs=1e-6)


def test_work_that_exactly_fills_a_window_lands_on_its_boundary():
    cal = S.ShiftCalendar()
    a, b = cal._windows(0)[0]
    assert cal.add_working_minutes(float(a), float(b - a)) == pytest.approx(float(b))


def test_prev_working_minute_treats_the_window_end_as_working():
    """[lo, hi) forward means `hi` is not a working minute; backwards it is
    exactly where work that ends there finished. Symmetry here puts every
    backward schedule one shift early."""
    cal = S.ShiftCalendar()
    _, b = cal._windows(0)[0]
    assert cal.prev_working_minute(float(b)) == pytest.approx(float(b))
    assert cal.next_working_minute(float(b)) > float(b)


def test_working_minutes_between_agrees_with_add():
    cal = S.ShiftCalendar()
    start = cal.next_working_minute(0.0)
    end = cal.add_working_minutes(start, 745.0)
    assert cal.working_minutes_between(start, end) == pytest.approx(745.0, abs=1e-6)


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    db = tmp_path_factory.mktemp("plan") / "p.db"
    conn = model.create(db)
    generate.seed_definitions(conn)
    return conn


@pytest.fixture(scope="module")
def jobs(seeded):
    orders = [(f"WO-{i:02d}", "BRKT-100" if i % 3 else "PLATE-200",
               20 + 5 * (i % 4), 900.0 + 180 * i, 0.0) for i in range(1, 13)]
    return P.jobs_from_db(seeded, orders)


def test_planner_reproduces_the_old_scheduler_without_constraints(seeded, jobs):
    """The claim the whole comparison rests on: `schedule_finite` is
    `schedule()` plus constraints, not a different algorithm that happens to
    give different numbers."""
    cap = P.capacity_of(seeded)
    for rule in S.DISPATCH_RULES:
        old = S.schedule([j.as_scheduling_job() for j in jobs], cap, rule)
        new = P.schedule_finite(jobs, cap, rule)
        assert new["finish"] == pytest.approx(old["finish"]), rule
        assert new["makespan"] == pytest.approx(old["makespan"]), rule


def test_routing_comes_from_the_database_not_a_literal(seeded, jobs):
    j = next(x for x in jobs if x.sku == "BRKT-100")
    assert [o.seq for o in j.ops] == [10, 20, 30, 40, 50]
    assert [o.cert for o in j.ops] == [None, "WELD-2", "CNC-1", None, "INSP-1"]
    assert j.ops[1].setup_min == pytest.approx(900 / 60)


def test_run_scales_with_quantity_and_setup_does_not(seeded):
    small = P.jobs_from_db(seeded, [("A", "BRKT-100", 1, 1e6, 0.0)])[0]
    big = P.jobs_from_db(seeded, [("B", "BRKT-100", 50, 1e6, 0.0)])[0]
    assert big.ops[1].run_min == pytest.approx(small.ops[1].run_min * 50)
    assert big.ops[1].setup_min == pytest.approx(small.ops[1].setup_min)


def test_setup_matrix_is_ordered_the_way_a_shop_floor_orders_it():
    m = P.SetupMatrix()
    op = P.Op(seq=20, wc="WC-WELD", run_min=10, setup_min=15, sku="BRKT-100")
    same = m.minutes("BRKT-100", op)
    cross = m.minutes("PLATE-200", op)
    first = m.minutes(None, op)
    assert same == 0.0
    assert cross > first > same


def test_constraints_can_only_make_the_schedule_worse(seeded, jobs):
    cap = P.capacity_of(seeded)
    pool = P.OperatorPool.from_db(seeded)
    base = P.schedule_finite(jobs, cap, "EDD")
    for label, st, op in (("setups", P.SetupMatrix(), None),
                          ("operators", None, pool),
                          ("both", P.SetupMatrix(), pool)):
        r = P.schedule_finite(jobs, cap, "EDD", None, st, op)
        assert r["makespan"] >= base["makespan"] - 1e-9, label


def test_operators_cost_more_than_setups_here(seeded, jobs):
    """Not a general law -- a property of this shop, and the reason it is worth
    reporting: two holders per certification is a tighter constraint than the
    changeover matrix, and no capacity number shows it."""
    cap = P.capacity_of(seeded)
    base = P.schedule_finite(jobs, cap, "EDD")["makespan"]
    setups = P.schedule_finite(jobs, cap, "EDD", None, P.SetupMatrix(), None)["makespan"]
    ops = P.schedule_finite(jobs, cap, "EDD", None, None,
                            P.OperatorPool.from_db(seeded))["makespan"]
    assert ops - base > 3 * (setups - base)


def test_an_uncoverable_certification_is_reported_not_absorbed(seeded, jobs):
    """Nobody certified is an unschedulable operation, not an infinite queue.
    Reporting it as a delay buries it in a mean."""
    pool = P.OperatorPool(certs={"OP-99": set()})
    r = P.schedule_finite(jobs, P.capacity_of(seeded), "EDD", None, None, pool)
    assert r["uncovered_operations"]
    assert {u["cert"] for u in r["uncovered_operations"]} == {
        "WELD-2", "CNC-1", "INSP-1"}


def test_attending_only_the_setup_beats_attending_throughout(seeded, jobs):
    cap = P.capacity_of(seeded)
    allp = P.OperatorPool.from_db(seeded, attends="all")
    setup_only = P.OperatorPool.from_db(seeded, attends="setup")
    a = P.schedule_finite(jobs, cap, "EDD", None, P.SetupMatrix(), allp)
    b = P.schedule_finite(jobs, cap, "EDD", None, P.SetupMatrix(), setup_only)
    assert b["makespan"] < a["makespan"]


def test_coverage_names_the_single_points_of_failure(seeded):
    cov = P.OperatorPool.from_db(seeded).coverage()
    assert set(cov) == {"WELD-2", "CNC-1", "INSP-1"}
    assert all(v <= 2 for v in cov.values())


# --- backward ---------------------------------------------------------------

def test_backward_release_plus_work_equals_the_due_date(seeded, jobs):
    cal = S.ShiftCalendar()
    j = jobs[0]
    b = P.backward_from_due(j, cal, P.SetupMatrix())
    assert b["schedule"][-1]["latest_end"] == pytest.approx(j.due)
    assert b["latest_release"] < j.due


def test_backward_flags_a_promise_that_cannot_be_met(seeded):
    j = P.jobs_from_db(seeded, [("RUSH", "BRKT-100", 40, 30.0, 0.0)])[0]
    b = P.backward_from_due(j, None, P.SetupMatrix())
    assert b["feasible"] is False
    assert b["latest_release"] < 0


def test_forward_lead_time_exceeds_the_infinite_capacity_promise(seeded, jobs):
    """The gap is queue -- time no routing document mentions."""
    rec = P.reconcile_forward_backward(
        jobs, P.capacity_of(seeded), "EDD", None, P.SetupMatrix(),
        P.OperatorPool.from_db(seeded))
    assert rec["mean_inflation"] > 1.0
    assert all(r["queue_minutes"] > -1e-6 for r in rec["per_job"])


# ---------------------------------------------------------------------------
# the terminal that writes
# ---------------------------------------------------------------------------

PIN = "8417"


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    db = tmp_path_factory.mktemp("srv") / "s.db"
    conn = model.create(db)
    generate.seed_definitions(conn)
    generate.seed_lots(conn)
    generate.run_week(conn, np.random.default_rng(0))
    conn.close()
    h = SV.serve(db)
    # Pass 5 made a PIN mandatory. These tests are about the WRITE path, so they
    # enrol real credentials and use them rather than switching authentication
    # off -- a test suite that runs with the security disabled stops testing the
    # thing that ships.
    for op in ("OP-01", "OP-05"):
        h["app"].credentials.enrol(op, PIN)
    yield h
    h["server"].shutdown()


def _call(url, path, body=None, tok=None):
    hdr = {"Content-Type": "application/json"}
    if tok:
        hdr["X-Session"] = tok
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, headers=hdr)
    try:
        with urllib.request.urlopen(req) as f:
            return f.status, json.load(f)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


@pytest.fixture(scope="module")
def weld_job(live):
    _, d = _call(live["url"], "/api/dispatch")
    w = [r for r in d if r["cert_required"] == "WELD-2"]
    assert w, "fixture needs a unit with a certified weld operation pending"
    return w[0]["unit_id"], w[0]["seq"]


def test_a_write_without_a_session_is_refused(live):
    c, b = _call(live["url"], "/api/complete",
                 {"unit_id": "x", "seq": 10, "wc_id": "WC-CUT"})
    assert c == 401 and "badge" in b["error"]


def test_unknown_operator_cannot_log_in(live):
    c, _ = _call(live["url"], "/api/login", {"op_id": "NOPE", "pin": PIN})
    assert c == 404


def test_uncertified_operator_is_refused_with_the_reason(live, weld_job):
    tok = _call(live["url"], "/api/login", {"op_id": "OP-05", "pin": PIN})[1]["token"]
    unit, seq = weld_job
    c, b = _call(live["url"], "/api/start",
                 {"unit_id": unit, "seq": seq, "wc_id": "WC-WELD"}, tok)
    assert c == 409
    assert b["kind"] == "CertificationError"
    assert "WELD-2" in b["error"] and "deviation" in b["error"]


def test_can_start_agrees_with_start(live, weld_job):
    """They call the same functions, which is the only way a greyed-out button
    and a refusal are guaranteed to say the same thing."""
    unit, seq = weld_job
    for op_id, expect in (("OP-05", False), ("OP-01", True)):
        tok = _call(live["url"], "/api/login", {"op_id": op_id, "pin": PIN})[1]["token"]
        _, dry = _call(live["url"],
                       f"/api/can_start?unit_id={unit}&seq={seq}", None, tok)
        assert dry["allowed"] is expect, op_id


def test_a_deviation_reference_overrides_the_certification(live, weld_job):
    tok = _call(live["url"], "/api/login", {"op_id": "OP-05", "pin": PIN})[1]["token"]
    unit, seq = weld_job
    c, b = _call(live["url"], "/api/start",
                 {"unit_id": unit, "seq": seq, "wc_id": "WC-WELD",
                  "deviation_ref": "DEV-77"}, tok)
    assert c == 200 and b["ok"] is True


def test_the_write_actually_lands_and_a_second_one_is_refused(live, weld_job):
    tok = _call(live["url"], "/api/login", {"op_id": "OP-01", "pin": PIN})[1]["token"]
    unit, seq = weld_job
    _call(live["url"], "/api/start",
          {"unit_id": unit, "seq": seq, "wc_id": "WC-WELD"}, tok)
    c1, _ = _call(live["url"], "/api/complete",
                  {"unit_id": unit, "seq": seq, "wc_id": "WC-WELD"}, tok)
    assert c1 == 200
    conn = model.connect(live["app"].db_path)
    n = conn.execute("SELECT COUNT(*) FROM op_record WHERE unit_id=? AND seq=? "
                     "AND action='COMPLETE'", (unit, seq)).fetchone()[0]
    conn.close()
    assert n == 1
    c2, b2 = _call(live["url"], "/api/complete",
                   {"unit_id": unit, "seq": seq, "wc_id": "WC-WELD"}, tok)
    assert c2 == 409
    # The application check fires before the UNIQUE index does. The index is the
    # backstop for code that does not come through here, not the path.
    assert b2["kind"] in ("ConservationError", "AlreadyRecorded")


def test_a_missing_field_is_a_400_and_names_what_is_missing(live):
    tok = _call(live["url"], "/api/login", {"op_id": "OP-01", "pin": PIN})[1]["token"]
    c, b = _call(live["url"], "/api/complete", {"unit_id": "x"}, tok)
    assert c == 400 and "seq" in b["error"] and "wc_id" in b["error"]


def test_an_internal_error_is_a_500_with_a_body_not_a_dropped_socket(live):
    """Without the guard, a handler bug reaches the client as "remote end closed
    connection without response" and every fault looks like the network."""
    c, b = _call(live["url"], "/api/unit?unit_id=NO-SUCH-UNIT")
    assert c in (404, 500) and "error" in b


def test_the_server_enforces_nothing_itself(live, weld_job, monkeypatch):
    """Every rule lives in `execution.py`. The proof is that relaxing the rule
    THERE relaxes it at the HTTP boundary too -- if the server held a copy, this
    request would still be refused."""
    unit, seq = weld_job
    tok = _call(live["url"], "/api/login", {"op_id": "OP-05", "pin": PIN})[1]["token"]
    c, _ = _call(live["url"], "/api/start",
                 {"unit_id": unit, "seq": seq, "wc_id": "WC-WELD"}, tok)
    assert c == 409
    monkeypatch.setattr(ex, "check_certification", lambda *a, **k: None)
    c2, _ = _call(live["url"], "/api/start",
                  {"unit_id": unit, "seq": seq, "wc_id": "WC-WELD"}, tok)
    assert c2 == 200, "the server refused on its own authority, not execution.py's"


# ---------------------------------------------------------------------------
# real connections
# ---------------------------------------------------------------------------

ROOT = pathlib.Path(__file__).resolve().parents[2]
HIST = ROOT / "data1-oee-platform" / "out" / "historian.db"
ML1 = ROOT / "ml1-rul-platform" / "out" / "completion.json"


def test_missing_feeds_are_reported_not_defaulted(tmp_path):
    out = I.connect_all(tmp_path / "nope.db", tmp_path / "nope.json")
    assert out["historian"]["available"] is False
    assert out["ml1"]["available"] is False
    assert "no historian" in out["historian"]["why"]


def test_an_unknown_work_centre_fails_open_and_is_recorded():
    g = I.gate_operation("WC-GRIND", {})
    assert g["allowed"] is True and g["state"] == "UNKNOWN"
    assert "recorded" in g["reason"]


def test_a_stale_reading_is_allowed_but_distinguished_from_a_good_one():
    """The outcome that only exists once the feed is real. A hard-coded dict is
    never stale, so the interface version could not have had this case."""
    states = {"WC-WELD": {"state": "STALE", "machines": [], "oldest_age_s": 9e5}}
    g = I.gate_operation("WC-WELD", states)
    assert g["allowed"] is True
    assert g["state"] == "STALE" and g["severity"] == "warn"
    assert I.gate_operation("WC-CUT", {})["severity"] == "info"


def test_a_work_centre_is_down_only_when_every_machine_in_it_is(tmp_path):
    db = tmp_path / "h.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE samples (machine TEXT, tag TEXT, value TEXT, "
                 "source_ts TEXT, status TEXT)")
    now = dt.datetime.now(dt.timezone.utc)
    ts = now.isoformat()
    conn.executemany("INSERT INTO samples VALUES (?,?,?,?,?)", [
        ("MC-201", "State", '"UNPLANNED_DOWN"', ts, "Good"),
        ("MC-202", "State", '"RUNNING"', ts, "Good"),
        ("MC-103", "State", '"UNPLANNED_DOWN"', ts, "Good"),
    ])
    conn.commit()
    conn.close()
    st = I.HistorianFeed(db).work_centre_states(now=now)["work_centres"]
    assert I.gate_operation("WC-MACH", st)["allowed"] is True    # 1 of 2 down
    assert I.gate_operation("WC-WELD", st)["allowed"] is False   # its only one


def test_an_unmapped_machine_is_reported_rather_than_dropped(tmp_path):
    db = tmp_path / "h2.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE samples (machine TEXT, tag TEXT, value TEXT, "
                 "source_ts TEXT, status TEXT)")
    now = dt.datetime.now(dt.timezone.utc)
    conn.execute("INSERT INTO samples VALUES ('MC-999','State','\"RUNNING\"',?,'Good')",
                 (now.isoformat(),))
    conn.commit()
    conn.close()
    st = I.HistorianFeed(db).work_centre_states(now=now)
    assert st["unmapped_machines"] == ["MC-999"]


@pytest.mark.skipif(not HIST.exists(), reason="DATA-1 has not been run")
def test_the_real_historian_parses():
    f = I.HistorianFeed(HIST)
    assert f.available
    states = f.latest_states()
    assert states and all(isinstance(v[0], str) for v in states.values())
    newest = max(dt.datetime.fromisoformat(t) for _, t in states.values())
    fresh = f.work_centre_states(now=newest + dt.timedelta(seconds=1))
    assert fresh["work_centres"] and not fresh["unmapped_machines"]


@pytest.mark.skipif(not ML1.exists(), reason="ML-1 has not been run")
def test_only_a_production_model_raises_work_orders():
    r = I.RulFeed(ML1)
    assert r.production_model()["stage"] == "Production"
    a = r.alarms(["WC-WELD"])
    assert a["ok"] and a["alarms"][0]["model_version"] is not None


def test_no_production_model_means_no_work_orders(tmp_path):
    p = tmp_path / "ml.json"
    p.write_text(json.dumps({"registry": {"index": [
        {"name": "m", "version": 1, "stage": "Staging", "fingerprint": "x"}]}}))
    a = I.RulFeed(p).alarms(["WC-WELD"])
    assert a["ok"] is False and "staging" in a["why"].lower()


def test_priority_comes_from_the_pessimistic_end_of_the_interval(tmp_path):
    """A median of 24 against a threshold of 24 is PLANNED; the same forecast
    read at its 5th percentile is URGENT. Planning from a median means being
    wrong half the time, and it is the half where the machine fails first."""
    p = tmp_path / "ml.json"
    p.write_text(json.dumps({
        "registry": {"index": [{"name": "m", "version": 2,
                                "stage": "Production", "fingerprint": "f"}]},
        "intervals": {"median": {"point": 24.0, "width": 6.0},
                      "p05": {"point": 16.9}}}))
    a = I.RulFeed(p).alarms(["WC-WELD"], urgent_below=24.0)
    assert a["alarms"][0]["predicted_rul"] == 24.0
    assert a["alarms"][0]["priority"] == "URGENT"


def test_a_retrained_model_can_raise_a_new_order_for_the_same_asset(tmp_path):
    """Deduplicating on source alone means a retrained model can never escalate
    an asset that already has an open order, and "the new model says this is now
    urgent" is exactly the message that must get through."""
    conn = sqlite3.connect(tmp_path / "m.db")
    v1 = [{"asset": "WC-WELD", "source": "ML-1", "predicted_rul": 60.0,
           "rul_p05": 50.0, "priority": "PLANNED", "model_name": "m",
           "model_version": 1, "model_fingerprint": "a"}]
    v2 = [dict(v1[0], model_version=2, priority="URGENT", rul_p05=12.0)]
    assert I.raise_maintenance_orders(conn, v1)["created"] == 1
    assert I.raise_maintenance_orders(conn, v1)["deduped"] == 1
    assert I.raise_maintenance_orders(conn, v2)["created"] == 1
    n = conn.execute("SELECT COUNT(*) FROM maintenance_wo").fetchone()[0]
    conn.close()
    assert n == 2
