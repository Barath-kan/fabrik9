# Explainability by Construction

How FABRIK-9 explains its agents' decisions, and — more importantly — the rule
that governs what it is *allowed* to say.

## The governing principle

> **Rule (Faithful Explanation).** Every explanation surfaced to the user must
> correspond to information the planner explicitly computed during normal
> execution. The explanation layer may read, reformat, and route that
> information; it must never infer, approximate, or fabricate decision
> rationale the planner did not itself produce.

The negative form is the operative one. If the system shows a reason, that
reason was *computed* — not reconstructed after the fact to look plausible. A
confident-sounding explanation that does not match the actual decision is worse
than silence: it manufactures trust the system has not earned.

This is a project-wide invariant, not a UI concern. Two earlier phases enforced
the same guarantee from the opposite direction — *behavior must not silently
drift* — because an explanation is only faithful if the thing it describes is
itself stable and honestly reported.

### Phase 3 — optimization that changed speed, not behavior

The A* pathfinding rewrite cut search cost ~2.9× under load
([PERF_REPORT.md](PERF_REPORT.md)). It was allowed to ship only because it
provably moved no decision. Proof is a pinned fingerprint: a SHA-256 over the
full sorted-key state snapshot (`Simulation.to_dict()`), which folds in the
nodes-expanded and heap-operation counters alongside every grid cell, agent,
and economy field. Before and after the optimization the hash is identical
(`tests/test_fingerprint.py`, pin `3dda234ec8f28f0f`); `test_determinism`
separately asserts equal `stats["nodes"]` and `stats["heap_ops"]` across
independent runs. Node counts, heap-op counts, and full state hash — all
identical. A faster planner that decided anything differently would have failed
this test.

### Phase 2 — failure handling scoped to protect determinism

The authoritative tick loop swallows I/O failures (a dropped socket, a
transient DB error) and recovers, because those are expected and non-fatal. It
deliberately does **not** swallow exceptions from `sim.step()`. The simulation
core is deterministic and bit-reproducible, so an exception there is a real
logic bug, and continuing would run the loop on corrupted, partially-mutated
state. Such an exception propagates and crashes the loop loudly (logged
CRITICAL by the task's done-callback);
`test_tick_loop_does_not_swallow_sim_core_failure` pins that boundary. A
silently-corrupted sim would turn every downstream explanation into a lie —
refusing to continue is precisely how the Faithful Explanation rule is kept
upstream of the explanation layer.

## Fidelity levels

Every user-facing explanation falls into one of three tiers. The first two are
permitted; the third is explicitly out of scope. The discipline of the whole
layer lives in the boundary between tier 2 and tier 3.

| Tier | Definition | Real example |
|------|------------|--------------|
| **1 — Direct observation** | Read verbatim from state the planner computed. | The winning task, each candidate's utility score, the per-candidate rejection reason, and the idle reason are written onto the agent (`decision_pick`, `last_decision`, `idle_reason`) *as the auction runs*, then displayed unchanged. |
| **2 — Derived from executed control flow** | A sound inference from what the trace shows *did and did not happen* — not a stored field, but strictly entailed by the recorded execution. | The Decision Tree's **"not attempted"** state. The auction walks candidates best-first and stops at the first success, so a candidate ranked below the winner was never evaluated. It is neither *reachable* nor *blocked* — the algorithm never asked. Reporting it as a distinct third state says exactly what the control flow entails, and refuses to claim a verdict that was never computed. |
| **3 — Out of scope** | Would require *new computation*, not exposure of existing computation. | Counterfactual cross-agent substitution, sensitivity/weight analysis, and emergent/global behavior explanation. None is computed during normal execution, so none may be shown. |

Tier 2 exposes what the executed trace already entails. Tier 3 would have to run
something the planner never ran. Conflating the two is exactly the failure the
governing rule forbids.

## What's implemented

Every item below is tier 1 or tier 2 — each value traces to something the
planner actually computed.

| Feature | Tier | Source of truth |
|---------|------|-----------------|
| **Decision trace with scores** | 1 | Top candidates and their utility scores, straight from `last_decision` / `decision_pick`. |
| **Three-state decision tree** | 1 + 2 | *Succeeded* and *rejected-as-unroutable* are observed (tier 1); *not attempted* is the control-flow inference above (tier 2). |
| **Rejection reasons** | 1 | Why each evaluated-but-passed-over candidate lost ("no viable route"), recorded as the auction rejects it. |
| **Idle reasons** | 1 | The distinct causes of an agent standing by (nothing pending vs. pending-but-unreachable vs. all-reserved), set where the auction resolves to SUPERVISE/IDLE. |
| **Decision timeline** | 1 | Task lifecycle (created → assigned → done, plus faults) reconstructed only from event-log entries and `decision_pick` the server already emits. |
| **Factory goals** | 1 | Derived from live state the server sends — open faults, assembler buffers, miner count, idle agents — each goal a direct readout. |
| **Template narration** | 1 | Plain-language lines filled from the same computed state (see below). |
| **Tile inspector** | 1 | Reads the `world.grid` cell already on the client; pure exposure of authoritative state. |
| **Click-to-target fault injection** | 1 | The server runs the *same* A* an agent would use to reach the clicked tile (`_agent_can_reach`) before creating the fault. "No agent can reach this location" is the literal result of a real pathfinding computation, not a guess. |

## Explicitly deferred — and why each needs new computation, not exposure

Each item is tier 3: deferred not because it is hard to *display*, but because
the planner does not *compute* it during normal execution. Surfacing it
faithfully would mean building the computation first.

- **Counterfactual reasoning** ("agent A2 would have been the better choice").
  The auction is agent-centric: each agent bids on tasks from its own position.
  A cross-agent counterfactual requires task-centric bidding — every task scored
  against every agent — which is a different scheduling architecture, not a
  field waiting to be surfaced.
- **Sensitivity / weight analysis** ("the decision would flip if distance
  mattered 20% less"). This requires re-running the scorer with perturbed inputs
  and comparing outcomes. The single forward pass the planner actually runs
  carries no such gradient.
- **Emergent / global behavior explanation** ("throughput dipped because three
  miners starved one assembler"). This is a time-series pattern over history the
  sim does not currently instrument. Detecting it means adding new analysis, not
  reading an existing value.

Deferring these *is* the principle working as intended: rather than ship a
plausible-sounding approximation, the system declines to explain what it did not
compute.

## Why templates, not an LLM, for narration

The narration layer is deliberately template-based, filled from computed state,
and **not** LLM-generated — for two reasons that both reduce to the governing
rule.

1. **Fidelity.** An LLM would produce fluent prose that reads like an
   explanation but is under no constraint to match the decision the planner
   actually made. That is exactly the failure the Faithful Explanation rule
   exists to prevent, and the most dangerous kind, because it is the most
   convincing.
2. **Reproducibility.** Narration is keyed to sim ticks and filled from seeded,
   deterministic state, so an identical seed yields an identical account of the
   run. A sampled language model would narrate the same seed differently each
   time, breaking the reproducibility the rest of the system guarantees.

For a project whose entire claim is *faithful, reproducible explanation*, a
plausible-but-unfaithful sentence is strictly worse than no sentence at all.
