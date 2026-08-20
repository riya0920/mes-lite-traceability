# SE-2 — MES-Lite: Work Order Execution & Traceability

**Status: ~20% slice.** The domain model, the execution rules with their refusals,
consumption-at-operation genealogy, rework re-entry, and the one-command recall
drill are built and verified against planted ground truth. Scheduling, capacity,
the operator UI, and concurrency testing are not.

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
scales with `1 + rework entries at that operation`.

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

## What is NOT built (the other 80%)

1. **No concurrency testing.** The spec asks for two operators completing the same
   serialised unit's operation with one winning cleanly. SQLite serialises writers,
   so the *test* would pass and would prove nothing about a real deployment. The
   double-completion guard exists and is enforced, but it is a read-then-write
   without a transaction boundary and would race under a real concurrent database.
   Calling this "race-safe" would be the overclaim; it is not.
2. **No scheduling or capacity.** Work centres have a `capacity` column that
   nothing reads. No dispatch list, no finite-capacity scheduling, no shift
   calendar, no queue at a work centre.
3. **No lot-tracked product actually exercised.** `PLATE-200` is defined as
   lot-tracked with its own routing, and the `unit.lot_qty` dual model exists, but
   the generator only runs the serialised product. The dual model is therefore
   *designed* and not *demonstrated*.
4. **No UI.** No operator terminal, no dispatch screen, no disposition queue.
   Everything is a Python call.
5. **No integration with the other specs.** DATA-1's machine states should gate
   operations (a down machine blocks work) and ML-1's alarms should create
   maintenance work orders. Neither is wired.
6. **No property-based testing.** The spec explicitly asks for the rework state
   machine to be property-tested. It is exercised by a deterministic generator
   with 4 rework events, which is coverage, not proof.
7. **Audit posture is "AS9100-aware", not compliant.** Every transaction carries
   operator, timestamp and workstation, and the log is append-only by convention —
   but there is no electronic signature, no record retention policy, no controlled
   document linkage, and no validation package.
8. **33 units, one week.** The transactions/second figure is measured on a tiny
   database with warm caches, and the recall query runs against 123 genealogy
   edges. Both numbers would change shape at millions of rows, where the index
   design starts to matter.

## Layout

```
src/model.py       SQLite schema: routings, BOM-at-operation, lots, units, genealogy, audit
src/execution.py   the rules and their refusals; quantity-conservation invariant
src/trace.py       forward/backward genealogy, split-tree walk, recall drill, birth certificate
src/generate.py    a week of execution with planted violations and a planted recall scenario
run_mes.py         orchestration; writes docs/RESULTS.md
```
