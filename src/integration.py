"""Real connections to DATA-1 and ML-1, over files rather than imports.

The README said these were interfaces rather than connections, and gave the
reason: "a cross-project import is the thing that makes two systems impossible to
deploy separately". That reason is right, and importing DATA-1's Python would
still be wrong. What was missing is the third option: consume the artefact each
project already PUBLISHES, over a contract that is a schema rather than a
signature.

  DATA-1 writes `out/historian.db` -- a `samples` table of
  (machine, tag, value, source_ts, status). SE-2 opens it read-only and derives
  machine state from the `State` tag. The contract is four column names.

  ML-1 writes `out/completion.json` -- a registry index of
  (name, version, stage, fingerprint) and bootstrap prediction intervals. SE-2
  reads the index, and refuses to act on anything not in Production.

Neither project imports the other, neither needs the other to run, and either can
be absent -- which is the case the fake version could not have, because a
hard-coded dict is never absent and never stale.

STALENESS IS THE POINT. The interface version gated on a dict of machine states
that was always present, always current and always right. A real feed is none of
those. A state that is three days old is not a state, and a gate that cannot tell
the difference between "the weld cell is running" and "the weld cell was running
on Friday" is worse than no gate: it is a green light with no information behind
it. So the policy has three outcomes rather than two, and the middle one --
stale -- is the one that only exists once the connection is real.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3

# DATA-1's machine ids are not SE-2's work-centre ids, and pretending they are is
# the integration bug that takes a week to find. The map is explicit, incomplete
# on purpose, and an unmapped machine is reported rather than dropped.
MACHINE_TO_WC = {
    "MC-101": "WC-CUT", "MC-102": "WC-CUT",
    "MC-103": "WC-WELD",
    "MC-201": "WC-MACH", "MC-202": "WC-MACH",
    "MC-203": "WC-PAINT",
}

DOWN_STATES = {"UNPLANNED_DOWN", "PLANNED_STOP", "SETUP"}


def _parse_ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


class HistorianFeed:
    """Latest machine state from DATA-1's historian, with a freshness budget.

    Opened read-only (`mode=ro`), which is not a detail: SE-2 has no business
    writing to another system's historian, and a URI-mode connection makes that
    a property of the connection rather than a rule somebody has to remember.
    """

    def __init__(self, path, max_age_s: float = 900.0,
                 machine_map: dict | None = None):
        self.path = pathlib.Path(path)
        self.max_age_s = float(max_age_s)
        self.map = dict(machine_map or MACHINE_TO_WC)
        self.available = self.path.exists()
        self._conn = None
        if self.available:
            try:
                self._conn = sqlite3.connect(
                    f"file:{self.path.as_posix()}?mode=ro", uri=True)
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("SELECT 1 FROM samples LIMIT 1")
            except sqlite3.Error as e:
                self.available = False
                self.why = f"historian present but unusable: {e}"
        if not self.available and not hasattr(self, "why"):
            self.why = f"no historian at {self.path}"

    def latest_states(self) -> dict:
        """machine -> (state, source_ts). The newest `State` row per machine."""
        if not self.available:
            return {}
        rows = self._conn.execute(
            "SELECT machine, value, source_ts FROM samples "
            "WHERE tag='State' AND status='Good' "
            "  AND source_ts = (SELECT MAX(s2.source_ts) FROM samples s2 "
            "                   WHERE s2.machine = samples.machine "
            "                     AND s2.tag='State' AND s2.status='Good')")
        out = {}
        for r in rows:
            v = r["value"]
            try:                       # DATA-1 stores tag values as JSON
                v = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                pass
            out[r["machine"]] = (str(v), r["source_ts"])
        return out

    def work_centre_states(self, now: dt.datetime | None = None) -> dict:
        """Roll machine states up to work centres.

        A work centre is DOWN only when EVERY machine in it is down. One of two
        CNC machines being down is reduced capacity, not a stopped cell, and
        gating on the worst machine would block a work centre that is running.
        """
        now = now or dt.datetime.now(dt.timezone.utc)
        per_machine = self.latest_states()
        wc: dict = {}
        unmapped = []
        for m, (state, ts) in per_machine.items():
            target = self.map.get(m)
            if target is None:
                unmapped.append(m)
                continue
            try:
                age = (now - _parse_ts(ts)).total_seconds()
            except (ValueError, TypeError):
                age = float("inf")
            wc.setdefault(target, []).append(
                {"machine": m, "state": state, "age_s": age,
                 "stale": age > self.max_age_s})
        out = {}
        for target, ms in wc.items():
            fresh = [m for m in ms if not m["stale"]]
            if not fresh:
                out[target] = {"state": "STALE", "machines": ms,
                               "oldest_age_s": max(m["age_s"] for m in ms)}
            elif all(m["state"] in DOWN_STATES for m in fresh):
                out[target] = {"state": fresh[0]["state"], "machines": ms,
                               "oldest_age_s": max(m["age_s"] for m in fresh)}
            else:
                running = next(m for m in fresh if m["state"] not in DOWN_STATES)
                out[target] = {"state": running["state"], "machines": ms,
                               "oldest_age_s": max(m["age_s"] for m in fresh)}
        return {"work_centres": out, "unmapped_machines": sorted(unmapped),
                "n_machines": len(per_machine), "available": self.available}


def gate_operation(wc_id: str, states: dict) -> dict:
    """Should an operation be allowed to start at this work centre?

    Three outcomes, and the middle one is what the real connection added:

      DOWN     the feed says the cell is stopped -> blocked, with the state named
      STALE    the feed has an answer and it is too old to believe -> ALLOWED,
               and recorded distinctly, because an old reading is not evidence of
               a stopped machine and blocking on it hands the plant to the
               message bus
      UNKNOWN  no reading at all -> allowed and recorded, the policy that was
               already documented

    Fail-open on both non-answers is deliberate and it is a real trade: the cost
    is production recorded against a machine that was genuinely down while the
    feed was broken. The alternative -- fail closed -- stops the plant every time
    a broker restarts, which is how an integration gets switched off permanently
    and then the gate protects nothing at all.
    """
    row = states.get(wc_id)
    if row is None:
        return {"allowed": True, "state": "UNKNOWN", "severity": "info",
                "reason": f"{wc_id}: no equipment state -- allowed, and recorded"}
    st = row["state"]
    if st == "STALE":
        return {"allowed": True, "state": "STALE", "severity": "warn",
                "age_s": row["oldest_age_s"],
                "reason": (f"{wc_id}: newest equipment state is "
                           f"{row['oldest_age_s'] / 60:.0f} min old -- allowed, "
                           "and recorded as unverified")}
    if st in DOWN_STATES:
        return {"allowed": False, "state": st, "severity": "block",
                "reason": f"{wc_id} is {st}"}
    return {"allowed": True, "state": st, "severity": "ok", "reason": ""}


# ---------------------------------------------------------------------------
# ML-1
# ---------------------------------------------------------------------------

class RulFeed:
    """ML-1's published registry and prediction intervals.

    Two rules that only become possible once the feed is real:

      AN ALARM MUST NAME ITS MODEL. A maintenance work order raised by a model
      is a claim, and a claim with no provenance cannot be reviewed. The work
      order records model name, version and fingerprint, so "why did we pull that
      machine" has an answer six months later.

      ONLY PRODUCTION MODELS RAISE WORK ORDERS. ML-1's registry has a `stage`,
      and a staging model's alarm reaching maintenance is how a test run becomes
      a truck roll.

    And the interval is carried, not just the point estimate. A predicted RUL of
    24 cycles whose 5th percentile is 17 is a different work order from one whose
    5th percentile is 23, and a feed that passes only the point value has thrown
    away the difference before anyone can act on it.
    """

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.available = self.path.exists()
        self.doc = {}
        if self.available:
            try:
                self.doc = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                self.available = False
                self.why = f"ML-1 output present but unreadable: {e}"
        if not self.available and not hasattr(self, "why"):
            self.why = f"no ML-1 output at {self.path}"

    def production_model(self) -> dict | None:
        idx = (self.doc.get("registry") or {}).get("index") or []
        prod = [m for m in idx if m.get("stage") == "Production"]
        if not prod:
            return None
        return max(prod, key=lambda m: m.get("version", 0))

    def intervals(self) -> dict:
        return (self.doc.get("intervals") or {})

    def alarms(self, assets: list, urgent_below: float = 24.0) -> dict:
        """Turn ML-1's fleet interval into per-asset alarms.

        ML-1 publishes a FLEET-level interval, not a per-asset one, and that is
        stated rather than papered over: every asset here gets the same predicted
        RUL, so this demonstrates the provenance and dispositioning path and does
        NOT demonstrate per-asset prediction. Wiring per-asset RUL needs ML-1 to
        publish per-unit rows, which it does not.
        """
        model = self.production_model()
        if model is None:
            return {"ok": False, "alarms": [],
                    "why": ("no Production model in ML-1's registry -- refusing "
                            "to raise work orders from a staging model")}
        iv = self.intervals()
        med = iv.get("median") or {}
        p05 = iv.get("p05") or {}
        point = float(med.get("point", 0.0))
        lo = float(p05.get("point", point))
        out = []
        for a in assets:
            out.append({
                "asset": a,
                "predicted_rul": point,
                "rul_p05": lo,
                "interval_width": float(med.get("width", 0.0)),
                # Priority off the PESSIMISTIC end. Planning maintenance from a
                # median means being wrong half the time, and the half you are
                # wrong is the half where the machine fails first.
                "priority": "URGENT" if lo < urgent_below else "PLANNED",
                "model_name": model.get("name"),
                "model_version": model.get("version"),
                "model_fingerprint": model.get("fingerprint"),
                "source": "ML-1",
            })
        return {"ok": True, "alarms": out, "model": model,
                "basis": "fleet interval, not per-asset"}


def raise_maintenance_orders(conn, alarms: list) -> dict:
    """Insert maintenance work orders, deduplicated per (asset, model version).

    Keyed on the model VERSION rather than just the source: deduplicating on
    source alone means a retrained model can never raise a new work order for an
    asset that already has one, and "the new model says this is now urgent" is
    exactly the message that must get through.
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS maintenance_wo (
        mwo_id INTEGER PRIMARY KEY AUTOINCREMENT, asset TEXT NOT NULL,
        source TEXT NOT NULL, predicted_rul REAL, rul_p05 REAL,
        confidence REAL, priority TEXT NOT NULL, created_ts TEXT NOT NULL,
        model_name TEXT, model_version INTEGER, model_fingerprint TEXT,
        UNIQUE(asset, source, model_version))""")
    created, deduped = 0, 0
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for a in alarms:
        try:
            conn.execute(
                "INSERT INTO maintenance_wo (asset, source, predicted_rul, "
                "rul_p05, priority, created_ts, model_name, model_version, "
                "model_fingerprint) VALUES (?,?,?,?,?,?,?,?,?)",
                (a["asset"], a["source"], a["predicted_rul"], a.get("rul_p05"),
                 a["priority"], now, a.get("model_name"),
                 a.get("model_version"), a.get("model_fingerprint")))
            created += 1
        except sqlite3.IntegrityError:
            deduped += 1
    conn.commit()
    return {"created": created, "deduped": deduped}


def connect_all(historian_path, ml1_path, max_age_s: float = 900.0,
                now: dt.datetime | None = None) -> dict:
    """Both feeds, with whatever is missing reported rather than defaulted."""
    hist = HistorianFeed(historian_path, max_age_s=max_age_s)
    rul = RulFeed(ml1_path)
    states = hist.work_centre_states(now=now) if hist.available else {
        "work_centres": {}, "unmapped_machines": [], "available": False}
    return {
        "historian": {"available": hist.available,
                      "why": getattr(hist, "why", None),
                      "path": str(hist.path), **states},
        "ml1": {"available": rul.available, "why": getattr(rul, "why", None),
                "path": str(rul.path),
                "model": rul.production_model() if rul.available else None},
        "_feeds": {"historian": hist, "rul": rul},
    }
