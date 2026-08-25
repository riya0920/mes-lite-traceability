# SE-2 — MES-Lite: Work Order Execution & Traceability

**Status: complete.** The domain model, the execution rules with their refusals,
consumption-at-operation genealogy, rework re-entry, and the one-command recall
drill are built and verified against planted ground truth, along with the
lot-tracked model, property-based rework testing and a dispatch list. Finite
scheduling, the operator UI, and concurrency testing are not.

```bash
python run_mes.py
```

~0.4 seconds. Writes [docs/RESULTS.md](docs/RESULTS.md), `out/results.json`, and
`out/birth_certificate.txt`.

## The recall drill

Planted scenario, so the answer is known before the query runs: lot **L-4471** is
split into `L-4471-A` (180 units) and `L-4471-B` (120). A goes to WO-1001, B to
WO-1002, and WO-1003 runs on clean `L-4998` as a control group.

| | |
|---|---|
| lots pulled into scope | L-4471, L-4471-A, L-4471-B |
| units affected | **20** |
| expected (ground truth) | 20 |
| **missed** | **0** |
| **false positives** | **0** |
| control group leaked in | 0 |
| already shipped | 10 — Northwind Rail, Cascade Transit |
| finished on hand | 9 |
| already scrapped | 1 |
| **query time** | **0.7 ms** |

**The split is what separates a working recall from a plausible one.** A query
that stops at the named lot — `SELECT ... WHERE lot_id='L-4471'` — returns
**0 units**. Not "fewer": zero. After the split, no consumption row cites L-4471
at all, because every issue names a child lot. That query comes back clean, the
recall is closed, and 20 units stay in the field.

`trace.affected_lots()` walks *up* to the root of the split tree and back *down*
through every child, which is why it returns 20. Walking up matters as much as
down: if the recalled id is itself a split child, its siblings came from the same
original material.

## Consumption at operation, not at order

The genealogy edge is created when lot L is issued to work order W **at operation
N**. This is the modelling decision the whole project turns on.

Suppose WO-1001 builds 300 units, consuming bar stock at operation 20, and draws
from L-4471 for the first 180 and L-4998 for the rest. The supplier recalls
L-4471:

- **order-level consumption**: the order touched L-4471, so all 300 units are
  suspect
- **operation-level**: the 180 units that actually consumed it are suspect

A 40% difference in exposure, which in a recall is the number of customers who get
a letter. Order-level consumption cannot do better, because it never recorded
which units were built while which lot was mounted.

## Every planted violation, blocked

| rule | attempted | blocked |
|---|---|---|
| start op 30 before ops 10/20 exist | 1 | **1/1** |
| uncertified operator at a WELD-2 operation | 1 | **1/1** |
| issue 3× the BOM quantity | 1 | **1/1** |
| issue a component the routing does not consume there | 1 | **1/1** |
| complete the same operation twice, no rework entry | 1 | **1/1** |
| issue more than the lot has on hand | 1 | **1/1** |
| *authorised deviation (must be **allowed**)* | 1 | **allowed** ✔ |

The last row is the politically important one. Operations will ask to skip an
operation for a hot order, and both obvious answers are wrong: rigid refusal gets
the system bypassed on paper, and a silent bypass destroys the record. The third
option — a **deviation with an authorisation reference** — is allowed, is recorded
on the operation, and appears on that unit's build record forever.

## Quantity conservation, and the category I was missing

`started = completed + scrapped + nonconformances + in_process`, at every
operation, counted from the append-only ledger rather than from a status column —
because a status column is a cache and this is what the cache is supposed to agree
with. **0 violations across 17 (work order, operation) pairs.**

| work order | op | started | completed | scrapped | NCRs | rework entries | in process |
|---|---|---|---|---|---|---|---|
| WO-1001 | 10 | 12 | 12 | 0 | 0 | 0 | 0 |
| WO-1001 | 20 | 12 | 12 | 0 | 0 | 0 | 0 |
| WO-1001 | 30 | 12 | 11 | 1 | 0 | 0 | 0 |
| WO-1001 | 40 | 13 | 13 | 0 | 0 | 2 | 0 |
| WO-1001 | 50 | 13 | 11 | 0 | 2 | 0 | 0 |

The **nonconformances** column had to be added, and finding out why is the most
useful thing this project did. Without it, a unit that failed inspection at op 50,
was reworked, and came back to complete op 50 shows two starts and one completion
— a phantom in-process balance of 2 on units that were sitting on the shipping
dock. A pass that ended in an NCR was neither completed nor scrapped; it was
*dispositioned*, and that is a fourth accounting category, not a rounding error.

The **rework entries** column is the other half: a rework re-starts a unit at an
operation it already completed, so it adds to the started side. Note op 40 shows
13 starts against 11 units reaching it.

## Rework re-entry, and why it is the hard part

Rework is a first-class routing event (`REWORK_ENTRY` in `op_record`), not a
status flag. It has to be, for a specific reason: **the unit must be allowed to
complete an operation it has already completed**, and every naive precedence and
already-completed check refuses that — correctly, for a first pass. So
`complete_operation` compares against the most recent rework entry rather than
against all history.

Modelling rework as a status field is the common shortcut and it loses the
operation history, which destroys the answer to "how many times did this unit go
through op 40" — the first thing a quality engineer asks about a systemic defect.

**A bug this exposed.** The over-issue check was per unit lifetime, so the second
dose of powder on a repainted bracket read as a 200% over-issue and the system
refused it. That is not a strict system, it is an unusable one: the plant's
response is to issue the material against some other unit, and the genealogy
silently becomes fiction. The budget is now **per pass** — expected quantity
scales with `1 + rework entries at or before that operation`.

**And the second pass found that this fix was still half wrong.** "Rework entries
*at* that operation" was correct only for the one pattern the original generator
produced — reworking a single step back. A unit sent from op 40 back to op 10 runs
ops 10, 20 and 30 again, and each legitimately consumes its materials again; the
scope had to widen to `seq ≤ N`. Property testing with random re-entry points found
it on the first case. Same story for the completion boundary. Both are covered by
regression tests in `tests/test_rework_properties.py`.

**A second bug, in the generator rather than the model.** Rework was originally
left to a 12% coin flip per unit. It came up tails 27 times in a row, and the
first complete run shipped with the rework path never executed. Rework and scrap
are now planted deterministically, because ground truth you cannot count is not
ground truth.

## The birth certificate

Generated in **0.4 ms** per unit. A reworked unit shows the second pass through
op 40 and the second powder issue, which is exactly what an auditor is looking
for:

```
    op  40  START         by OP-05  at WC-PAINT 2026-07-01T08:11:22
    op  40  COMPLETE      by OP-05  at WC-PAINT 2026-07-01T08:11:22
    op  50  START         by OP-03  at WC-INSP  2026-07-01T08:14:27
    op  40  REWORK_ENTRY  by OP-03  at -        2026-07-01T08:14:27  (NCR-1)
    op  40  START         by OP-01  at WC-PAINT 2026-07-01T08:23:55
    op  40  COMPLETE      by OP-01  at WC-PAINT 2026-07-01T08:23:55
    op  50  START         by OP-03  at WC-INSP  2026-07-01T08:27:07
    op  50  COMPLETE      by OP-03  at WC-INSP  2026-07-01T08:27:07

  MATERIAL CONSUMED
    op  10  STEEL-BAR    lot L-4471-A     qty 1.0     supplier Meridian Steel (split from L-4471)
    ...
    op  40  POWDER       lot L-7003       qty 0.15    supplier ChromaCoat
    op  40  POWDER       lot L-7003       qty 0.15    supplier ChromaCoat
```

## Where MES ends and ERP begins

This is **ISA-95 level 3** — manufacturing operations management, i.e. execution.
Orders come *down* from ERP (level 4) with a quantity and a due date; completions,
consumption and scrap go *up*. Planning, costing, purchasing, MRP and the general
ledger are level 4 and are deliberately absent. A system that plans its own orders
is not an MES.

## Built in the second pass — see [docs/EXTENSIONS.md](docs/EXTENSIONS.md)

`python extend.py` — three gaps this README previously named, and the property
testing **found three real bugs in the execution rules**:

- **The lot-tracked model, actually run.** It was defined and never exercised.
  Running it immediately exposed that `scrap()` killed the whole batch on a partial
  scrap — 25 plates of 400 failing at drilling scrapped all 400.
- **Property-based rework testing.** 200 randomly generated routing histories with
  0–3 rework loops re-entering at random earlier operations: **0 property
  failures, 0 conservation violations, 294 rework entries**. Getting there required
  two more fixes, both invisible to the original generator because it only ever
  reworked one step back:
  - a rework at op N restarts the pass for **every** operation from N onward, so
    the over-issue budget and the completion boundary must both scope to `seq ≤ N`
  - an NCR consumes a START only if it **terminated** that pass; counting a
    post-completion NCR produced 12 units of *negative* work-in-process
- **A dispatch list**, derived entirely from the execution ledger so it cannot
  disagree with the record, sorted by hours per resource rather than unit count.

## Completed in the third pass — see [docs/COMPLETION.md](docs/COMPLETION.md)

```bash
python complete.py    # ~1 min (full run does 2M genealogy edges)
```

- **The concurrency test this README refused to fake.** It said the
  double-completion guard is a read-then-write with no transaction boundary and
  that calling it race-safe would be an overclaim. Correct — and now measured.
  Eight threads released off a barrier, completing the same operation:
  **1 accepted, 7 refused** with the boundary in place;
  **8 accepted** without the unique index. `BEGIN IMMEDIATE` takes
  the write lock before the read; the index makes the invariant true in the
  *schema*, which is what holds against a client that forgets the transaction.
  The unique key is (unit, seq, **pass**), because a reworked unit legitimately
  completes the same operation twice — the same insight the pass-2 rework bugs
  turned on.
- **Finite-capacity scheduling.** `work_center.capacity` had existed since pass 1
  and nothing read it. Finite-capacity flow time is
  **2.6× the infinite-capacity plan**, and
  **62% of it is queue time**. An
  infinite-capacity plan schedules three jobs onto one machine at 08:00 and
  reports a date that assumes they all ran — not optimistic, arithmetically
  impossible.
- **Four dispatch rules compared, and none dominates.** FIFO, SPT, EDD and
  critical ratio, scored on makespan, mean flow time, maximum lateness and
  tardiness. Choosing between them is choosing what the plant is judged on.
- **A shift calendar.** An 8-hour job started at
  14:00 takes **16.3 elapsed hours**
  (2.04×) because it crosses a break and the end of
  second shift.
- **Integration with DATA-1 and ML-1.** Equipment state gates operation starts
  (2 refused), and ML-1 alarms become maintenance work orders
  (2 created, 1 deduplicated).
  An unknown state **fails open and is recorded** — failing closed on a missing
  integration halts a plant because a message bus hiccupped, which is how an
  integration gets switched off permanently.
- **Electronic signatures with a stated meaning, and a hash-linked audit chain.**
  The README said the log was "append-only *by convention*", which is the
  load-bearing phrase: a table anyone can UPDATE is not an audit log. Each row
  now carries its predecessor's hash, so an in-place edit to one row is
  **detected** (at row 3). Signatures carry signer,
  timestamp and *meaning* — the third is the one that gets left out and the one
  that matters in a deposition.
- **The scale test.** 200,000 genealogy edges: the recall query takes
  **52 ms unindexed**,
  0.06 ms with an index on `lot_id`, and
  **0.03 ms with a covering index** on
  `(lot_id, unit_id)` — which contains the answer, so the query never touches the
  table. The README predicted the 0.7 ms figure would "change shape at millions
  of rows"; it does.
- **An operator terminal** at `out/terminal.html`, self-contained. It renders
  state and does **not** write, because a UI whose buttons silently no-op is
  worse than no UI — an operator who presses Complete and sees nothing will start
  keeping a paper log, which is the failure this project exists to prevent.

## Built in the fourth pass — see [docs/PLANNING_AND_CONNECTIONS.md](docs/PLANNING_AND_CONNECTIONS.md)

```bash
python run_pass4.py    # ~3 s
```

The last three items on the list below.

- **The planner uses the routing that is in the database.** `scheduling.py` took
  synthetic `(seq, work_centre, minutes)` triples while the schema beside it
  already had `std_setup_s`, `cert_required`, `work_center.capacity` and a
  `certification` table. `planning.schedule_finite` reads all of it — and with
  the constraints switched off it reproduces the old scheduler operation for
  operation on all four dispatch rules, which is what makes the difference
  attributable to the constraints rather than to a rewrite.
- **Operators cost 4× what setups cost**
  (+130 minutes against +34 on EDD), and no capacity number
  says so: CNC-1 has 2 holders, INSP-1 has 2 holders, WELD-2 has 2 holders,
  against five operators, one of whom holds nothing. A cell with three machines
  and two certified people has a capacity of two.
- **The naive plan promises zero late jobs.** SPT and EDD deliver
  everything on time without operators and miss
  5 and 6 of 12 with
  them. A planner whose constraints are optional produces a promise nobody can
  keep, in the most convincing possible form.
- **Backward scheduling**, which is the direction that says you are already
  late: a rush order promised in 30 minutes
  needs 538, so its latest
  release is **-508 minutes** —
  infeasible with every machine free, before anybody looks at a queue. Across
  the order book the finite forward pass takes
  **1.48×** the infinite-capacity promise,
  and that gap is queue.
- **The terminal writes.** A session model, and every write routed through
  `execution.py` — an uncertified operator gets a 409 carrying the reason and
  the override path, a deviation reference gets through, and a double-click
  leaves **1 completion** in `op_record`. That the
  server holds no rules of its own is tested by relaxing the check in
  `execution.py` and watching the same HTTP request start succeeding.
- **The integrations are connections.** Not by importing another project —
  by reading the artefact each one publishes. SE-2 opens DATA-1's
  `historian.db` read-only and derives machine state from the `State` tag, and
  reads ML-1's registry index to act only on a Production model
  (`rul-gbm` v4).

### And the connection found something on its first run

DATA-1's newest reading is **5.3 days old**, so every work centre gates
as `STALE` — a third outcome the interface version could not have, because a
hard-coded dict of machine states is never stale, never missing and never wrong.
A gate that cannot tell *the weld cell is running* from *the weld cell was
running on Friday* is worse than no gate: it is a green light with nothing
behind it. Stale fails **open**, and that is a trade rather than a convenience —
failing closed stops the plant every time a broker restarts, which is how an
integration gets switched off permanently.

Priority now comes from the pessimistic end of ML-1's interval: a median RUL of
24.0 cycles reads `PLANNED`,
and the same forecast at its 5th percentile (16.9)
reads `URGENT`. Planning from a median means being wrong half
the time, and it is the half where the machine fails first.

### A bug found building the backward walk

`add_working_minutes` compared `remaining <= avail`, both minute counts in the
tens of thousands, so work that exactly fills a shift window compares as *longer*
than the window by ~1e-12. The fall-through does not lose a picosecond — it
carries the residue into the **next** window and returns a time a whole shift
later, or after a weekend. 1052 of 4000 random round-trips failed before the
tolerance went in, and the forward function had carried the bug since pass 1.

## Also in the fifth pass — see [docs/ITERATED_PLANNING.md](docs/ITERATED_PLANNING.md)

```bash
python run_pass5.py
```

The backward pass was infinite-capacity, and the item said a real one *would need
the forward and backward passes to iterate*. It iterates now: each round feeds the
previous round's **measured queue** back into the backward pass as an allowance,
and the release dates that come out both order the forward pass (through a `PLAN`
dispatch rule) and control release — a job whose latest start is in the future is
held back rather than queued.

| release stagger | single: tardiness | late | iterated | late | better? |
|---:|---:|---:|---:|---:|:--:|
| 0 min | 238.3 | 6 | 724.4 | 7 | ❌ |
| 15 min | 295.8 | 7 | 395.4 | 6 | ❌ |
| 30 min | 116.5 | 2 | 77.6 | 3 | ✅ |
| 60 min | 43.5 | 2 | 0.0 | 0 | ✅ |
| 120 min | 0.0 | 0 | 0.0 | 0 | — |
| 240 min | 0.0 | 0 | 0.0 | 0 | — |

**It helps at [30, 60] minutes of stagger and hurts at [0, 15].**
At 60 minutes it removes the tardiness entirely; at zero it makes things three
times worse — and **every earlier pass in this project used the zero-stagger
instance**. With all twelve jobs available at time zero there is no release
*timing* to optimise: the shop is capacity-bound from the first minute, the only
lever is sequence, and EDD already sequences by the same information a backward
pass would produce. Holding a job back can only make it later.

The ablation says which half does the work: ordering alone reproduces the single
pass exactly (release order and due-date order are the same order here), so all
of the gain is the release control.

**It does not converge at any damping tested**, and that is reported rather than
smoothed. The allowances genuinely interact — releasing one job earlier changes
another's queue — so this is returned as a **search** with the best round kept
and a `converged` flag, not as a fixed-point algorithm.

### Two bugs that both made it look like it did nothing

The first version reordered the job list and handed it to a scheduler that
re-sorts by its own rule; twelve rounds measured the same schedule twelve times.
The second measured queue from the job's *original* release, so time a job was
deliberately held back counted as queue, released it earlier next round, and fed
back on itself — tardiness oscillated between 662 and 1,808 minutes over forty
rounds. Both have named tests.

## What is NOT built

1. **Not 21 CFR Part 11 compliant, and authentication does not change that.**
   The terminal now takes a badge and a PIN — PBKDF2-HMAC-SHA256 at 200k
   iterations, per-operator salt, constant-time comparison, lockout after five
   failures, session expiry — and serves over TLS. Part 11 wants an *identity
   lifecycle*: an authority issuing and revoking credentials, periodic access
   review, a password policy, and a validation package for the software that
   enforces them. This is the mechanism such a programme sits on; a mechanism
   without a programme is not compliance.
2. **The hash chain makes tampering detectable, not impossible.** An attacker
   who can rewrite the whole table can recompute the whole chain. What it defeats
   is the realistic case — a targeted edit to one inconvenient row — and it
   forces the harder case to leave traces in backups and replicas.
3. **The TLS certificate is self-signed and generated per run.** No CA, no
   revocation, and a client has to be handed the certificate out of band.
   **Client certificates are deliberately absent**: they authenticate machines,
   and a terminal authenticates people — issuing one per operator is the identity
   lifecycle above.
4. **No CSRF protection and no origin checking.** It matters the moment the
   terminal is served anywhere a browser can reach it from another page, and the
   fix is a same-site cookie plus an origin check rather than the bearer token
   used here. Lockout state also lives in process memory, so bouncing the service
   clears it.
5. **One process, one lock**, which serialises every write. Correct, and it does
   not scale.
6. **The iterated planner is a search, not a fixed point.** No damping tested
   converges, so it returns the best round of N with a `converged` flag rather
   than a settled plan. And it only pays when releases are staggered — on the
   all-available-at-once instance it is worse than a single pass.
7. **Sequencing is still non-delay and single-pass.** A machine never idles
   waiting for a better job, which is what a shop floor does and not always what
   is optimal, and nothing re-plans when reality diverges from the plan.
8. **ML-1 publishes a fleet interval, not per-asset RUL.** Every asset in the
   maintenance feed therefore carries the same prediction. The provenance and
   dispositioning path is real; per-asset prediction is not, and closing that
   needs ML-1 to publish per-unit rows.
9. **The equipment feed is a file, read on demand.** DATA-1's historian is polled
   rather than subscribed to, so freshness is whatever the last write left
   behind — which is exactly why the stale path exists and why it is exercised.
10. **Still one plant, one week of generated history.** The scale test grows the
    genealogy table to millions of rows to measure the query, but the *execution*
    path has never run at that size.

## Layout

```
src/model.py       SQLite schema: routings, BOM-at-operation, lots, units, genealogy, audit
src/execution.py   the rules and their refusals; quantity-conservation invariant
src/trace.py       forward/backward genealogy, split-tree walk, recall drill, birth certificate
src/generate.py    a week of execution with planted violations and a planted recall scenario
src/scheduling.py  shift calendar (both directions), dispatch rules, transaction boundary
src/planning.py    routing from the DB, setup matrix, operators, backward scheduling
src/server.py      the terminal's write path: sessions, and every write via execution.py
src/integration.py DATA-1's historian and ML-1's registry, read as published files
src/auth.py       badge+PIN credentials, lockout, session expiry, TLS contexts
run_mes.py         orchestration; writes docs/RESULTS.md
run_pass4.py       planning, the write path, the connections; writes the pass-4 doc
```
