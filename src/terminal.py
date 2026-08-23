"""The operator terminal, rendered.

WHAT AN OPERATOR TERMINAL HAS TO SHOW, and why each element earns its place. The
spec's red flag is an MES that is "a database with a web page on it", and the
difference is entirely in what the screen makes hard to get wrong:

  THE DISPATCH LIST, sorted by HOURS of work rather than unit count. A work
  centre with 3 jobs of 4 hours is busier than one with 12 jobs of 10 minutes,
  and a list sorted by count sends the supervisor to the wrong cell.

  THE ROUTING WITH PROGRESS. Which operations are done, which is next, and --
  critically -- whether this unit is on a REWORK pass. An operator who cannot see
  that a unit has been here before will repeat whatever was done last time.

  THE MATERIALS TO ISSUE AT THIS OPERATION, not at this order. Consumption is
  recorded at the operation, which is the modelling decision the whole project
  turns on, and a screen that lists the order's whole BOM invites the operator to
  book material at the wrong step and destroys the genealogy.

  THE BLOCKS, WITH REASONS. A greyed-out button with no explanation gets worked
  around within a shift. "You are not certified for WELD-2" gets a supervisor.

It renders state and does not write. Wiring it to `execution.py` needs a server,
and a UI whose buttons silently no-op would be worse than no UI at all.
"""
from __future__ import annotations

import html
import pathlib


def render(path, res: dict) -> dict:
    sc = res.get("scheduling", {})
    ig = res.get("integration", {})
    sg = res.get("signature", {})

    # A dispatch list from the schedule, in HOURS.
    rules = {r["rule"]: r for r in sc.get("rules", [])}
    wc_load: dict[str, float] = {}
    for jid, ops in (sc.get("schedule") or {}).items():
        for o in ops:
            wc_load[o["wc"]] = wc_load.get(o["wc"], 0.0) + o["minutes"] / 60.0
    if not wc_load:
        wc_load = {k: float(v) * 8 for k, v in (sc.get("capacity") or {}).items()}

    load_rows = "".join(
        f'<tr><td>{html.escape(wc)}</td><td class="n">{h:.1f}</td>'
        f'<td class="n">{(sc.get("capacity") or {}).get(wc, 1)}</td>'
        f'<td class="n">{h / max((sc.get("capacity") or {}).get(wc, 1), 1):.1f}</td></tr>'
        for wc, h in sorted(wc_load.items(), key=lambda kv: -kv[1]))

    gate_rows = "".join(
        f'<tr><td>{html.escape(a["wc"])}</td><td>{html.escape(a["state"])}</td>'
        f'<td>{"<span class=ok>start</span>" if a["allowed"] else "<span class=no>BLOCKED</span>"}</td>'
        f'<td>{html.escape(a["reason"] or "—")}</td></tr>'
        for a in ig.get("gate_attempts", []))

    mwo_rows = "".join(
        f'<tr><td>{html.escape(m["asset"])}</td><td>{html.escape(m["source"])}</td>'
        f'<td class="n">{m["predicted_rul"]:.0f}</td>'
        f'<td><b>{html.escape(m["priority"])}</b></td></tr>'
        for m in ig.get("maintenance_wos", []))

    rule_rows = "".join(
        f'<tr><td>{html.escape(r["rule"])}</td>'
        f'<td class="n">{r["mean_flow_time"]:.0f}</td>'
        f'<td class="n">{r["max_lateness"]:.0f}</td>'
        f'<td class="n">{r["n_late"]}</td></tr>' for r in sc.get("rules", []))

    # A worked routing card for one unit, including a rework pass, because that
    # is the case the screen has to make unmistakable.
    routing = [
        ("10", "OP-01", "WC-CUT", "done", "STEEL-BAR L-4471-A x1", 1),
        ("20", "OP-02", "WC-WELD", "done", "WIRE L-7001 x0.4, GAS L-7002 x2.0", 1),
        ("30", "OP-04", "WC-MACH", "done", "—", 1),
        ("40", "OP-05", "WC-PAINT", "done (pass 1)", "POWDER L-7003 x0.15", 1),
        ("50", "OP-03", "WC-INSP", "NCR — rework to op 40", "—", 1),
        ("40", "OP-01", "WC-PAINT", "NEXT (pass 2)", "POWDER L-7003 x0.15", 2),
    ]
    rt_rows = "".join(
        f'<tr class="{"next" if "NEXT" in st else ""}">'
        f'<td class="n">{s}</td><td>{html.escape(op)}</td>'
        f'<td>{html.escape(wc)}</td>'
        f'<td>{html.escape(st)}{" <span class=pass>pass " + str(p) + "</span>" if p > 1 else ""}</td>'
        f'<td class="mat">{html.escape(mat)}</td></tr>'
        for s, op, wc, st, mat, p in routing)

    doc = f"""<!doctype html>
<meta charset="utf-8"><title>Operator terminal</title>
<style>
:root{{--bg:#f7fafc;--fg:#1a202c;--card:#fff;--line:#e2e8f0;--mut:#718096;
       --ok:#2f855a;--no:#c53030;--warn:#b7791f}}
@media (prefers-color-scheme:dark){{:root{{--bg:#171923;--fg:#e2e8f0;--card:#242c3d;
 --line:#3a4459;--mut:#a0aec0;--ok:#68d391;--no:#fc8181;--warn:#f6c177}}}}
*{{box-sizing:border-box}}
body{{margin:0;padding:24px;background:var(--bg);color:var(--fg);
 font:14px/1.55 system-ui,sans-serif}}
h1{{font-size:20px;margin:0 0 2px}}
h2{{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--mut);
 margin:0 0 10px}}
.sub{{color:var(--mut);margin-bottom:20px}}
.grid{{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;
 padding:16px;overflow-x:auto}}
.wide{{grid-column:1/-1}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}}
th{{color:var(--mut);font-size:11px;text-transform:uppercase}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}}
td.mat{{color:var(--mut);font-size:12px}}
tr.next{{background:rgba(49,130,206,.10)}}
tr.next td{{font-weight:600}}
.ok{{color:var(--ok)}} .no{{color:var(--no);font-weight:600}}
.pass{{background:var(--warn);color:#111;font-size:10px;padding:1px 6px;
 border-radius:8px;margin-left:6px}}
.btns{{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}}
button{{padding:8px 14px;border:1px solid var(--line);background:transparent;
 color:inherit;border-radius:6px;font-size:13px;cursor:not-allowed;opacity:.75}}
.note{{font-size:12px;color:var(--mut);margin-top:10px}}
</style>
<h1>Operator terminal</h1>
<div class="sub">WO-1001-U07 &middot; BRKT-100 &middot; rendered by
 <code>complete.py</code> — read-only, see the note below</div>

<div class="grid">
  <div class="card wide">
    <h2>Routing and progress — unit WO-1001-U07</h2>
    <table><thead><tr><th class="n">seq</th><th>operator</th><th>work centre</th>
      <th>status</th><th>material at THIS operation</th></tr></thead>
      <tbody>{rt_rows}</tbody></table>
    <div class="btns">
      <button disabled>Start op 40</button>
      <button disabled>Issue POWDER L-7003</button>
      <button disabled>Complete</button>
      <button disabled>Raise NCR</button>
      <button disabled>Scrap</button>
    </div>
    <div class="note"><b>The pass-2 badge is the point of this screen.</b> An
     operator who cannot see that a unit has been here before will repeat
     whatever was done last time — and the material list is per OPERATION, not
     per order, because consumption is recorded at the operation and a screen
     showing the whole BOM invites booking it at the wrong step.</div>
  </div>

  <div class="card">
    <h2>Dispatch list — hours, not job count</h2>
    <table><thead><tr><th>work centre</th><th class="n">hours queued</th>
      <th class="n">machines</th><th class="n">hours/machine</th></tr></thead>
      <tbody>{load_rows}</tbody></table>
    <div class="note">Sorted by hours. A centre with 3 four-hour jobs is busier
     than one with 12 ten-minute jobs, and a list sorted by count sends the
     supervisor to the wrong cell.</div>
  </div>

  <div class="card">
    <h2>Equipment gate (from DATA-1)</h2>
    <table><thead><tr><th>work centre</th><th>state</th><th>start</th>
      <th>reason</th></tr></thead><tbody>{gate_rows}</tbody></table>
    <div class="note">A blocked button with no explanation gets worked around
     within a shift. The reason is what gets a supervisor instead.</div>
  </div>

  <div class="card">
    <h2>Maintenance work orders (from ML-1)</h2>
    <table><thead><tr><th>asset</th><th>source</th><th class="n">predicted RUL</th>
      <th>priority</th></tr></thead><tbody>{mwo_rows}</tbody></table>
  </div>

  <div class="card">
    <h2>Dispatch rule comparison</h2>
    <table><thead><tr><th>rule</th><th class="n">mean flow</th>
      <th class="n">max lateness</th><th class="n">late</th></tr></thead>
      <tbody>{rule_rows}</tbody></table>
    <div class="note">No rule dominates. Choosing is choosing what the plant is
     judged on.</div>
  </div>

  <div class="card wide">
    <h2>Records</h2>
    <p style="font-size:13px">{html.escape(sg.get('summary', ''))}</p>
    <div class="note"><b>This page renders state and does not write.</b> Wiring
     the buttons to <code>execution.py</code> needs a server, and a UI whose
     buttons silently no-op would be worse than no UI — an operator who presses
     Complete and sees nothing happen will conclude the system is broken and
     start keeping a paper log, which is the failure this project exists to
     prevent.</div>
  </div>
</div>
"""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(doc, encoding="utf-8")
    return {"path": str(p), "bytes": p.stat().st_size, "self_contained": True,
            "writes": False}
