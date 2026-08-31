# Agent Evaluation Framework

This document defines the reproducible evidence boundary for MapGo's Agent system. The older Single-vs-Multi replay benchmark remains useful as a deterministic regression test, but it is not treated as evidence of real-model quality.

## Modes

- `offline` runs the production orchestration, tool authorization, deterministic map provider, Planner/Solver and rule-based evaluation boundaries. Replan cases create an isolated database, persist a V1 plan and execute the production `DynamicReplanningOrchestrator`; they do not simulate replanning by running initial planning twice. It runs the full 180-case dataset in CI. Offline D/E/F use clearly labelled deterministic critic fixtures; their results are architecture regressions, not LLM claims.
- `live` calls the OpenAI-compatible endpoint configured by `LLM_BASE_URL` and uses `LLM_MODEL`, `LLM_SMALL_MODEL` and `LLM_STRONG_MODEL`. The map boundary remains deterministic so the ablation changes only the Agent/model configuration. If `LLM_API_KEY` is empty, the command writes a `SKIPPED` JSON and Markdown report. A selected F profile also requires an explicit `LLM_STRONG_MODEL`; the framework refuses to relabel `LLM_MODEL` as strong-model evidence. It never substitutes fake results.

No API key, prompt response or authorization header is written to an artifact. `AGENT_EVAL_PROVIDER` and `AGENT_EVAL_MODEL_VERSION` are optional non-secret provenance labels.

## Dataset

The source is `backend/tests/evaluation/datasets/agent_golden_v1.json`. Version `1.0.0` contains 24 scenario templates that deterministically expand to 180 uniquely identified cases. The report hashes the canonical expanded cases, rather than only hashing the template file, so changes to expansion semantics are detectable.

Coverage includes normal planning, missing requirements, multi-turn clarification, budget and time conflicts, closures, low-quality Provider evidence, walking risk, elderly/children, dietary restrictions, route edits, weather, deviation, tool failure/timeout, illegal JSON and hallucination probes, Critic false decisions, infeasibility, HITL, and replan/no-replan controls.

## Ablations

| ID | Configuration |
|---|---|
| A | Single Agent baseline |
| B | Multi-Agent without Critic |
| C | Multi-Agent with Rule Critic |
| D | Multi-Agent with LLM Critic |
| E | Multi-Agent with the configured small model |
| F | Multi-Agent with the configured strong model |

Every selected profile receives the same ordered cases and deterministic map/Solver boundary. The framework records observed results without asserting that any profile must win.

## Commands

CI/full offline evaluation:

```bash
python backend/tests/evaluation/agent_evaluation_framework.py --mode offline --suite full
```

Twenty-case live smoke across all ablations:

```bash
LLM_API_KEY=... python backend/tests/evaluation/agent_evaluation_framework.py --mode live --suite smoke
```

Manual full live run, or a lower-cost selected-profile run:

```bash
LLM_API_KEY=... python backend/tests/evaluation/agent_evaluation_framework.py --mode live --suite full
LLM_API_KEY=... python backend/tests/evaluation/agent_evaluation_framework.py --mode live --suite smoke --profiles A,C,F
```

Interview comparison protocol: 60 unique cases selected by deterministic category round-robin,
three repeats, and the A/C/F profiles. `--fail-on-skip` makes missing credentials visible to
automation; `--update-readme` replaces only the marked README block after a completed live run.

```bash
LLM_API_KEY=... LLM_STRONG_MODEL=... python backend/tests/evaluation/agent_evaluation_framework.py --mode live --suite full --profiles A,C,F --case-count 60 --repeats 3 --update-readme --fail-on-skip
```

Artifacts are timestamped JSON and Markdown files under `artifacts/agent-evaluation/` by default. Override that path with `AGENT_EVAL_OUTPUT_DIR` or `--output-dir`.

## Metric semantics

The JSON includes Task Success, Constraint Satisfaction, Hard Constraint Violation, tool selection/argument accuracy, illegal/unnecessary tool calls, retry, clarification, Critic recall/precision/false rejects, recovery, replanning, HITL precision, LLM calls, tokens, cost, P50/P95 latency and handoffs. It stores combined metrics, per-trial metrics, every case execution with its trial number, and a dedicated failure-case list. Dynamic cases also report production replay coverage, persisted DAG accuracy, execution mode, true Agent task count and deterministic stage count.

Every rate is represented as `{value, numerator, denominator}`. `value: null` means no selected case made the metric applicable; it is never silently converted to zero. Per-case evidence includes expected/actual tools, terminal state, fallback use and captured error. Run metadata includes UTC time, Git commit, dataset version/hash, Provider/model labels, case counts, token totals, cost and latency.

Cost is computed from the configured per-million-token rates. Strong-profile parser and Critic calls use the strong-tier rates rather than the compatibility-model rates. Provider invoices remain the authoritative billing source.

The Single Agent baseline keeps a direct-controller replan simulation for comparison and reports `production_dynamic_replay_rate=0`. Multi-Agent profiles execute the production dynamic path and must report one Replanner Agent task plus five deterministic stages per successful dynamic workflow. This distinction is deliberate and visible in every artifact.
