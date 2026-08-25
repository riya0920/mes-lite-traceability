"""Pass 5: iterating the forward and backward passes.

Two of these tests exist because the first two versions of this feature looked
like they did nothing, for reasons that had nothing to do with the algorithm:
the scheduler ignored the order it was handed, and the queue was measured from
the wrong release. Both are pinned.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import generate                # noqa: E402
import model                   # noqa: E402
import planning as P           # noqa: E402

_spec = importlib.util.spec_from_file_location("p5", ROOT / "run_pass5.py")
P5 = importlib.util.module_from_spec(_spec)
sys.modules["p5"] = P5
_spec.loader.exec_module(P5)

RESULT = ROOT / "out" / "pass5.json"


@pytest.fixture(scope="module")
def shop(tmp_path_factory):
    db = tmp_path_factory.mktemp("it") / "i.db"
    conn = model.create(db)
    generate.seed_definitions(conn)
    return {"conn": conn, "cap": P.capacity_of(conn),
            "sm": P.SetupMatrix(), "pool": P.OperatorPool.from_db(conn)}


def _jobs(shop, stagger, n=12, tight=1.5):
    return P5.build(shop["conn"], n, stagger, tight)


# --- the PLAN rule -----------------------------------------------------------

def test_the_plan_rule_actually_changes_the_schedule(shop):
    """The first version's bug: reorder the job list, hand it to a scheduler
    that re-sorts by EDD, and measure the same schedule every round.

    Zero stagger, because that is where the rule can matter: with jobs released
    60 minutes apart only one is available at a time, so any dispatch rule
    produces the same schedule and the test would pass against a rule that does
    nothing.
    """
    jobs = _jobs(shop, 0)
    fwd = P.schedule_finite(jobs, shop["cap"], "EDD")
    # reverse the priority: the last job by due date goes first
    prio = {j.job_id: -j.due for j in jobs}
    plan = P.schedule_finite(jobs, shop["cap"], P.PLAN_RULE, priority=prio)
    assert plan["finish"] != fwd["finish"], "PLAN made no difference"


def test_the_plan_rule_refuses_without_priorities(shop):
    with pytest.raises(ValueError, match="priority per job"):
        P.schedule_finite(_jobs(shop, 0), shop["cap"], P.PLAN_RULE)


def test_an_unknown_rule_is_still_refused(shop):
    with pytest.raises(ValueError, match="unknown dispatch rule"):
        P.schedule_finite(_jobs(shop, 0), shop["cap"], "NONSENSE")


# --- the feedback ------------------------------------------------------------

def test_queue_is_measured_from_the_release_actually_used(shop):
    """The second version's bug: measuring from the ORIGINAL release counts
    deliberately-held-back time as queue, which releases the job earlier next
    round -- positive feedback with no restoring force."""
    src = (ROOT / "src" / "planning.py").read_text(encoding="utf-8")
    assert "actual_release" in src
    assert 'flow = fwd["finish"][j.job_id] - j.released' not in src


def test_a_job_is_never_released_before_its_material(shop):
    """The backward pass can ask for a start in the past and the shop cannot
    supply one."""
    jobs = _jobs(shop, 60)
    for j in jobs:
        j.released = 100.0
    r = P.iterate_passes(jobs, shop["cap"], setups=shop["sm"],
                         operators=shop["pool"], max_rounds=3)
    assert r["best"]["finish"]
    for jid, fin in r["best"]["finish"].items():
        own = sum(o.run_min for j in jobs if j.job_id == jid for o in j.ops)
        assert fin >= 100.0 + own - 1e-6, jid


def test_damping_must_be_in_range(shop):
    for bad in (0.0, -0.5, 1.5):
        with pytest.raises(ValueError, match="damping"):
            P.iterate_passes(_jobs(shop, 0), shop["cap"], damping=bad)


def test_it_reports_whether_it_converged(shop):
    """A planner that silently returns round 20 of an oscillation is worse than
    one that says it did not settle."""
    r = P.iterate_passes(_jobs(shop, 60), shop["cap"], setups=shop["sm"],
                         operators=shop["pool"], max_rounds=6)
    assert "converged" in r and isinstance(r["converged"], bool)
    assert r["rounds"] <= 6
    assert len(r["history"]) == r["rounds"]
    assert r["best"]["round"] <= r["rounds"]


def test_the_best_round_is_the_best_round(shop):
    r = P.iterate_passes(_jobs(shop, 60), shop["cap"], setups=shop["sm"],
                         operators=shop["pool"], max_rounds=8)
    assert r["best"]["total_tardiness"] == min(
        h["total_tardiness"] for h in r["history"])


# --- the finding -------------------------------------------------------------

def test_iterating_helps_with_staggered_releases_and_not_without(shop):
    """The finding, and the reason the sweep is the experiment: with everything
    available at time zero there is no release TIMING to optimise."""
    flat = P.iterate_passes(_jobs(shop, 0), shop["cap"], setups=shop["sm"],
                            operators=shop["pool"], max_rounds=20, damping=0.5)
    stag = P.iterate_passes(_jobs(shop, 60), shop["cap"], setups=shop["sm"],
                            operators=shop["pool"], max_rounds=20, damping=0.5)
    assert not flat["improved"], "it should not help when nothing is staggered"
    assert stag["improved"], "it should help when releases are spread"


def test_release_control_is_the_half_that_does_the_work(shop):
    jobs = _jobs(shop, 60)
    with_ctl = P.iterate_passes(jobs, shop["cap"], setups=shop["sm"],
                                operators=shop["pool"], max_rounds=20,
                                control_release=True)
    order_only = P.iterate_passes(jobs, shop["cap"], setups=shop["sm"],
                                  operators=shop["pool"], max_rounds=20,
                                  control_release=False)
    assert with_ctl["best"]["total_tardiness"] < order_only["best"]["total_tardiness"]
    # ordering alone reproduces the single pass on this instance, because the
    # release order and the due-date order are the same order
    assert order_only["best"]["total_tardiness"] == pytest.approx(
        order_only["single_pass"]["total_tardiness"])


@pytest.mark.skipif(not RESULT.exists(), reason="run run_pass5.py first")
def test_the_reported_sweep_shows_both_signs():
    d = json.loads(RESULT.read_text(encoding="utf-8"))
    sw = d["stagger"]
    assert sw["helped_at"], "nothing improved anywhere"
    assert sw["hurt_at"], "nothing got worse -- the finding needs both signs"
    assert 0 in sw["hurt_at"], "zero stagger should be the case that gets worse"


@pytest.mark.skipif(not RESULT.exists(), reason="run run_pass5.py first")
def test_the_document_says_it_does_not_converge():
    d = json.loads(RESULT.read_text(encoding="utf-8"))
    assert d["damping"]["any_converged"] is False
    doc = (ROOT / "docs" / "ITERATED_PLANNING.md").read_text(encoding="utf-8")
    assert "does not converge" in doc
    assert "search" in doc
