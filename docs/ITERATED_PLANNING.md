# Iterating the forward and backward passes

The README's item: *backward scheduling is infinite-capacity … a genuine backward pass over a contended shop is a harder problem that would need the forward and backward passes to iterate.* It iterates now, and the result is more interesting than "it works".


## How the loop closes

The backward pass is missing one thing: how much **queue** each job will meet. The forward pass measures exactly that. So each round feeds the previous round's measured queue back into the backward pass as an allowance, and the release dates that come out do two jobs:

- they **order** the forward pass, through a `PLAN` dispatch rule
- they **control release**: a job whose latest start is in the future is held back rather than queued — input-output control, and the actual shop-floor use of a backward pass


### Two bugs on the way, both of which made it look like it did nothing

**The first version reordered the job list and handed it to a scheduler that re-sorts by EDD.** Twelve rounds measured the same schedule twelve times and reported no improvement — correctly, and for a reason that had nothing to do with the algorithm. That is what the `PLAN` rule is for.

**The second measured queue from the job's original release**, not the release actually used. Time a job was deliberately held back counted as queue, which released it earlier next round, which put it in the queue sooner, which raised everybody's measured queue: positive feedback with no restoring force. Tardiness oscillated between 662 and 1,808 minutes over forty rounds.


## When iterating helps, and when it hurts

12 jobs, due dates at 1.5× their own work content, sweeping how far apart the jobs are **released**:

| release stagger | single pass: tardiness | late | iterated: tardiness | late | better? |
|---:|---:|---:|---:|---:|:--:|
| 0 min | 238.3 | 6 | 724.4 | 7 | ❌ |
| 15 min | 295.8 | 7 | 395.4 | 6 | ❌ |
| 30 min | 116.5 | 2 | 77.6 | 3 | ✅ |
| 60 min | 43.5 | 2 | 0.0 | 0 | ✅ |
| 120 min | 0.0 | 0 | 0.0 | 0 | — |
| 240 min | 0.0 | 0 | 0.0 | 0 | — |

**Iterating helps at [30, 60] minutes of stagger and hurts at [0, 15].** At 60 minutes it removes the tardiness entirely — 43.5 minutes across two late jobs becomes zero. At zero stagger it makes things three times worse.

**The explanation is structural, and it is why the sweep is the experiment.** Every earlier pass in this project used the zero-stagger instance: all twelve jobs available at time zero. There is no release *timing* to optimise there — the shop is capacity-bound from the first minute, the only lever is sequence, and EDD already sequences by the same information a backward pass would produce. Holding a job back can only make it later.

Give the releases some spread and the lever appears: a job held out of the queue is a job not adding to everyone else's waiting, which is the entire argument for input-output control. Past 120 minutes the shop is no longer contended and there is nothing left to fix.


## Which half does the work

| variant | tardiness | late |
|---|---:|---:|
| with release control | 0.0 | 0 |
| ordering only | 43.5 | 2 |
| single pass, for reference | 43.5 | — |

**It is the release control, not the ordering.** Ordering by backward release date alone reproduces the single pass exactly — because on this instance the release order and the due-date order are the same order, so the `PLAN` rule and EDD produce the same schedule. All of the gain comes from holding jobs back.


## It does not converge, and that is reported rather than hidden

| damping | converged | best tardiness | first move | last move | shrinking? |
|---:|:--:|---:|---:|---:|:--:|
| 1.0 | no | 0.0 | 75.6 | 50.8 | yes |
| 0.7 | no | 0.0 | 75.6 | 45.4 | yes |
| 0.5 | no | 0.0 | 75.6 | 44.4 | yes |
| 0.3 | no | 0.0 | 75.6 | 47.9 | yes |
| 0.1 | no | 0.0 | 75.6 | 49.9 | yes |

**No damping tested reaches a fixed point.** The allowances keep moving because they genuinely interact: releasing one job earlier changes another's queue, and there is no reason a heuristic feedback on twelve coupled jobs should have a stable point at all.

So this is reported as a **search**, not as a fixed-point algorithm. The best round is kept and returned, and `converged` is in the result so a caller cannot mistake round 20 of an oscillation for an answer. A planner that silently returns the last round of a wobble is worse than one that says it did not settle.


## What this settles

- **The item is built**: the two passes iterate, with release control and a dispatch rule that actually uses the plan.
- **It is worth doing only when releases are staggered**, and the instance this project had been using is the one case where it is not. That is a finding about the instance as much as about the method.
- **It is a search, not a fixed point.** Nothing here converges, and presenting a best-of-N as though it had converged would be the more flattering and less true description.
- **Still one shop and one week.** Twelve jobs on five work centres is not evidence about scheduling in general.

