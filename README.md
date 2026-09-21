# MES-Lite: Work Order Execution & Traceability

## What it is

A small **Manufacturing Execution System (MES)**: the software that runs the factory
floor between the business plan and the machines.

It tracks each work order as it moves through its steps. It records who did each
step, on which machine, and which batch (lot) of material went into each unit. It
blocks steps that break the rules.

The main question it answers is the one a plant gets when a supplier calls:
**"This batch of steel was bad. Which of our products contain it, and where are
they now?"**

## What we did

1. **Built the data model** in SQLite: products, routings (the ordered steps),
   work centers, material lots, units, operator certifications, and an audit log.
2. **Wrote the execution rules**, each with a clear refusal: steps in order, only
   certified operators, no over-issuing material, no doing a step twice.
3. **Recorded material at the step it was used**, not just at the order. This is
   what makes the recall answer precise.
4. **Handled rework**: a unit that fails inspection goes back to an earlier step
   and runs again, with its full history kept.
5. **Built a one-command recall drill** and tested it against a planted scenario
   where the right answer was known in advance.
6. **Added scheduling**: finite machine capacity, setups, operator availability,
   shift calendars, and forward and backward planning.
7. **Added an operator terminal** (web page) with badge + PIN login, TLS, and a
   tamper-evident audit log.
8. **Connected it to two sister projects**: machine state from the [Plant Data Platform](https://github.com/riya0920/plant-oee-platform) and
   maintenance alerts from the [Predictive Maintenance Platform](https://github.com/riya0920/predictive-maintenance-rul-platform).

Work was done in five passes. Each pass re-tested the claims of the one before,
and several found real bugs.

## Results

**Recall drill (planted scenario, answer known in advance)**

| | |
|---|---|
| affected units found | **20 of 20** |
| missed | **0** |
| false positives | **0** |
| already shipped | 10 (two customers named) |
| query time | **0.7 ms** |

A simple query on the recalled lot finds **0 units**, because the lot had been
split into two child lots and every use names a child. Our trace walks the split
tree and finds all 20.

**Rules**

- Every planted rule violation was blocked (6 of 6).
- The approved exception (a skip with a sign-off reference) was allowed and
  recorded.
- Quantity balance held at every step: **0 violations** in 17 checks.
- 200 random rework histories: **0 failures**.

**Scaling and safety**

- 8 threads trying to complete the same step at once: **1 accepted, 7 refused**.
  Without the database guard, all 8 got through.
- Recall query on 200,000 records: **52 ms** without an index, **0.03 ms** with one.
- Editing one row of the audit log is **detected**.

**Scheduling**

- With real machine limits, jobs take **2.6x** longer than the "infinite
  capacity" plan says. **62%** of that time is waiting in queue.
- Operator shortages cost about **4x** more time than machine setups.
- A plan that ignores operators says 0 late jobs. With operators, **5 to 6 of 12**
  are late.

**Bugs found by testing (and fixed)**

- Rework was refused as "over-issuing" material on the second pass.
- Rework that went back more than one step broke the checks. Random testing found
  this on the first try.
- A partial scrap on a batch scrapped the whole batch (all 400 plates for 25 bad).
- A rounding error in the shift calendar pushed some jobs a whole shift late
  (1,052 of 4,000 random checks failed before the fix).
- A test-data coin flip meant rework never actually ran in the first version.

## Key decisions and why

**Record material at the step, not at the order.**
If an order used two lots, order-level tracking marks every unit suspect. In our
example that is 300 units instead of the 180 that really used the bad lot. That is
40% more customers getting a recall letter.

**Trace recalls up and down the split tree.**
Lots get split. Tracing only the named lot misses everything. Tracing up to the
parent also catches sibling lots made from the same material.

**Rework is its own event, not a status.**
A status field loses the history. A quality engineer's first question is "how
many times did this unit go through step 40?", so every pass is kept.

**Count quantities from the log, not from a status column.**
The log is the truth. A status column is just a cached copy that has to agree
with it. This is how we found that failed inspections needed their own category.

**Allow exceptions, but record them.**
Refusing everything gets the system bypassed on paper. Silently allowing it
ruins the record. A sign-off reference does both jobs.

**Enforce "complete once" in the database, not just the code.**
A unique index still holds when some other client forgets the transaction.

**If a connected system is stale or missing, fail open and log it.**
Stopping the plant every time a message bus hiccups gets the integration
switched off for good. On its first real run, the Plant Data Platform's latest reading was 5.3
days old, so every machine correctly showed as `STALE`.

**Plan from the pessimistic end of the maintenance forecast.**
The median predicted life (24 cycles) says "planned". The 5th percentile (16.9)
says "urgent". Planning from the median means being wrong half the time.

**Stay in the MES layer.**
Planning, costing, purchasing and accounting belong to ERP. They are left out on
purpose.

**Report what did not work.**
Iterating the forward and backward planning passes helps when job releases are
staggered (30-60 min) and makes things worse when all jobs start at once. It
never settles on one answer, so it is returned as a best-of search, not claimed
as converged.

## Limits

- **Not 21 CFR Part 11 compliant.** Login, signatures and the audit chain are the
  mechanism. Compliance also needs processes for issuing and reviewing access.
- The audit chain makes edits **detectable, not impossible**.
- TLS certificate is self-signed. No CSRF or origin checks yet.
- One process, one write lock. Correct, but it does not scale.
- The Predictive Maintenance Platform gives one forecast for the whole fleet, not one per machine.
- Test data is one plant and one generated week. The execution path has not run
  at millions of rows (only the recall query has).

## How to run

```bash
pip install -r requirements.txt
python run_mes.py      # core system + recall drill, ~0.4 s
python extend.py       # lot model, rework property tests, dispatch list
python complete.py     # concurrency, scheduling, e-signatures, scale, ~1 min
python run_pass4.py    # planning with operators, terminal writes, connections
python run_pass5.py    # iterated planning
pytest
```

Full write-ups: [RESULTS](docs/RESULTS.md) ·
[EXTENSIONS](docs/EXTENSIONS.md) · [COMPLETION](docs/COMPLETION.md) ·
[PLANNING_AND_CONNECTIONS](docs/PLANNING_AND_CONNECTIONS.md) ·
[ITERATED_PLANNING](docs/ITERATED_PLANNING.md)

## Layout

```
src/model.py        database schema
src/execution.py    the rules and their refusals
src/trace.py        genealogy, recall drill, unit build record
src/generate.py     a week of test data with planted problems
src/scheduling.py   shift calendar, dispatch rules
src/planning.py     planning from the real routing, operators, backward pass
src/server.py       operator terminal; every write goes through execution.py
src/auth.py         badge + PIN login, lockout, TLS
src/integration.py  reads the Plant Data Platform and Predictive Maintenance outputs
```
