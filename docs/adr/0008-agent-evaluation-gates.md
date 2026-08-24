# ADR-0008: Deterministic Agent evaluation and CI quality gates

## Status

Accepted.

## Context

Schema-valid Agent output can still be wrong: intent fields may be omitted, routes may violate time limits, duplicate a POI, travel excessively far, or ignore an explicit preference. Prompt reviews and a few example tests do not provide a repeatable release gate.

LLM-as-judge alone is not suitable for hard route correctness because its score can drift with model versions and cannot replace deterministic constraint validation.

## Decision

Create two versioned offline evaluation suites:

1. Intent evaluation runs committed natural-language cases through the fallback parser and measures structured validity plus task-count, transport, deadline, `avoid_hiking`, and `travel_style` accuracy.
2. Route evaluation runs committed plan snapshots through the same deterministic evaluator used by the runtime Critic.

The route quality formula is:

```text
final = distance_reasonability * 0.40
      + time_reasonability     * 0.30
      + preference_match       * 0.30
```

Any hard failure sets the final score to zero regardless of component scores. Hard failures include non-success/empty plans, missing or duplicate Provider POI IDs, task deadline or latest-return violations, total-duration/walking/cost violations, evaluation time limits, and distance at least twice the case-specific reasonable limit. A route passes only without hard failures and with a final score of at least 75.

Explicit preferences are conjunctive: one missed preference cannot be averaged away by easier preference checks. The initial preference evaluator covers hiking avoidance, relaxed travel style, walking minimization, and high-rating selection.

The runtime Critic embeds the deterministic component/final score and hard-failure codes in `ReviewReport`. Server findings override an LLM Critic on hard failures. Online score and hard-failure metrics use bounded labels.

CI runs intent, role-isolation, and route suites. Current gates require 100% committed route expectation accuracy, 100% hard-failure detection, a minimum passing-route score of 85, at least 95% hiking-avoidance intent accuracy, and at least 90% travel-style accuracy.

## Consequences

- Route correctness gates are reproducible and independent of an external model.
- Offline and runtime Critic checks cannot silently diverge.
- Every new preference or constraint should add positive, negative, and adversarial golden cases.
- Case-specific distance/time targets remain benchmark policy, not universal geographic truth.
- Live LLM parsing and real-user satisfaction still require separate sampled evaluation; the committed suite primarily prevents deterministic regressions.
