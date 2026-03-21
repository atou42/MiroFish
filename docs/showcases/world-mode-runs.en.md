# World Mode Showcase

This page promotes real world runs from this fork into shareable, reviewable showcase material instead of leaving everything trapped in local `backend/uploads/` artifacts.

## Case 1: 240-tick long-running world progression

- Simulation ID: `sim_8ac60f042d62`
- Diagnostics Label: `final240_autorun_clean`
- Completed At: `2026-03-21T13:29:52`
- Ticks: `240`
- Actions Log Rows: `3594`
- Actor Profile: `eval_aliyun_qwen35_flash`
- Resolver Profile: `eval_litellm_gpt54_deep`

### Key Metrics

- Accepted Intents: `142`
- Deferred Intents: `77`
- Rejected Intents: `28`
- Resolver Salvaged: `0`
- Resolver Zero-Accept Diagnostics: `0`
- Accepted Event Sources:
  - `llm`: `138`
  - `llm_invalid_json_recovered`: `4`

### Final World State

- Tension: `0.95`
- Stability: `0.50`
- Momentum: `0.95`

Last-round summary:

> Tick 240 closed with 3 actor intents, 2 accepted events, 1 active event, and 4 queued events. Global tension remained at 0.95 while the world stayed highly kinetic rather than stalled.

### Why This Case Matters

- The world runtime can sustain long-horizon progression, not just short demos
- checkpoint, diagnostics, and reporting still remain usable after a large run
- `Qwen 3.5 Flash actor + GPT-5.4 deep resolver` proved viable for a serious reviewed run with low salvage dependence

## Case 2: operator state-bridge smoke

- Simulation ID: `sim_world_supervised_smoke_20260321_172346`
- Diagnostics Label: `operator_bridge_smoke`
- Completed At: `2026-03-21T17:28:55`
- Ticks: `2`
- Actions Log Rows: `20`
- Actor Profile: `eval_aliyun_qwen35_flash`
- Resolver Profile: `eval_litellm_gpt54_deep`

### Key Metrics

- Accepted Intents: `2`
- Deferred Intents: `0`
- Rejected Intents: `0`
- Resolver Salvaged: `0`
- Intent Sources:
  - `llm`: `3`
  - `llm_invalid_json_partial_recovered`: `1`

### Final World State

- Tension: `0.779`
- Stability: `0.215`
- Momentum: `0.762`

Last-round summary:

> Tick 2 ended with 2 new actor intents, 2 queued follow-up events, 2 completed events from tick 1, and a visibly intensified but still trackable world state.

### Why This Case Matters

- the operator CLI path is no longer "subprocess-only" and opaque to the service layer
- `run_state.json` now updates during execution
- `run_state.json`, `checkpoint.json`, and `world_run.py status` converge after completion
- the operator path can now be smoke-tested with real live-state visibility instead of only checking return codes

## Current Default Practice

If you are starting a new world run, the practical order is:

1. choose a preset
2. do a short smoke first
3. move into staged runs, for example `8 -> 16 -> 32`
4. generate diagnostics at each stage
5. promote strong runs into eval suites or case sets

## Recommended Presets

| Preset | Meaning |
|--------|---------|
| `recommended_throughput` | default throughput-oriented preset |
| `recommended_stable` | better for important runs and reviewability |
| `smoke_benchmark_minimax` | smoke-only, not the long-run default |

See:

- [../../backend/evals/world_runtime_presets.json](../../backend/evals/world_runtime_presets.json)
- [../../backend/evals/README.md](../../backend/evals/README.md)
- [../../backend/evals/world_model_eval_playbook.md](../../backend/evals/world_model_eval_playbook.md)
