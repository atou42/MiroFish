# World Model Eval

`backend/scripts/eval_world_models.py` benchmarks world-mode model selectors by cloning a base `simulation_config.json` into isolated run directories and scoring the resulting timeline.

## Suites

- `world_model_eval_suite.json`
  - 1 tick latency smoke.
  - Best for first-event speed, stall risk, salvage dependence, and immediate timeline cleanliness.
- `world_model_eval_progression_suite.json`
  - 3 tick progression mini.
  - Best for sustained advancement, queue promotion, event completion, and realized world-state movement.
- `world_case_sets/*.json`
  - Durable promoted case packs extracted from the completed 2026-03-19 campaign.
  - These files are directly runnable because they keep the standard suite format and only add lineage metadata.

## Commands

List configured selector profiles:

```bash
/Users/atou/MiroFish/backend/.venv/bin/python /Users/atou/MiroFish/backend/scripts/eval_world_models.py --list-selectors
```

List suite cases:

```bash
/Users/atou/MiroFish/backend/.venv/bin/python /Users/atou/MiroFish/backend/scripts/eval_world_models.py \
  --suite-config /Users/atou/MiroFish/backend/evals/world_model_eval_suite.json \
  --list-cases
```

Run a 1-tick latency smoke:

```bash
/Users/atou/MiroFish/backend/.venv/bin/python /Users/atou/MiroFish/backend/scripts/eval_world_models.py \
  --base-config /Users/atou/MiroFish/backend/uploads/simulations/sim_8ac60f042d62/simulation_config.json \
  --suite-config /Users/atou/MiroFish/backend/evals/world_model_eval_suite.json \
  --python-bin /Users/atou/MiroFish/backend/.venv/bin/python
```

Run a 3-tick progression mini:

```bash
/Users/atou/MiroFish/backend/.venv/bin/python /Users/atou/MiroFish/backend/scripts/eval_world_models.py \
  --base-config /Users/atou/MiroFish/backend/uploads/simulations/sim_8ac60f042d62/simulation_config.json \
  --suite-config /Users/atou/MiroFish/backend/evals/world_model_eval_progression_suite.json \
  --python-bin /Users/atou/MiroFish/backend/.venv/bin/python
```

Run the full staged campaign:

```bash
/Users/atou/MiroFish/backend/.venv/bin/python /Users/atou/MiroFish/backend/scripts/run_world_model_eval_campaign.py \
  --base-config /Users/atou/MiroFish/backend/uploads/simulations/sim_8ac60f042d62/simulation_config.json \
  --campaign-config /Users/atou/MiroFish/backend/evals/world_model_eval_campaign.json \
  --python-bin /Users/atou/MiroFish/backend/.venv/bin/python
```

List durable case sets:

```bash
/Users/atou/MiroFish/backend/.venv/bin/python /Users/atou/MiroFish/backend/scripts/build_world_eval_suite_from_case_set.py --list
```

Run the recommended progression case set directly:

```bash
/Users/atou/MiroFish/backend/.venv/bin/python /Users/atou/MiroFish/backend/scripts/eval_world_models.py \
  --base-config /Users/atou/MiroFish/backend/uploads/simulations/sim_8ac60f042d62/simulation_config.json \
  --suite-config /Users/atou/MiroFish/backend/evals/world_case_sets/recommended_progression.json \
  --python-bin /Users/atou/MiroFish/backend/.venv/bin/python
```

Materialize a filtered repeat suite from a case set:

```bash
/Users/atou/MiroFish/backend/.venv/bin/python /Users/atou/MiroFish/backend/scripts/build_world_eval_suite_from_case_set.py \
  --case-set-id recommended_stability \
  --case-ids recommended-throughput,recommended-stable \
  --repeat-count 5 \
  --output /tmp/world_recommended_stability_repeat5.json
```

Force three repeats for stability:

```bash
/Users/atou/MiroFish/backend/.venv/bin/python /Users/atou/MiroFish/backend/scripts/eval_world_models.py \
  --base-config /Users/atou/MiroFish/backend/uploads/simulations/sim_8ac60f042d62/simulation_config.json \
  --suite-config /Users/atou/MiroFish/backend/evals/world_model_eval_suite.json \
  --repeat-count 3 \
  --python-bin /Users/atou/MiroFish/backend/.venv/bin/python
```

Run the staged matrix eval:

```bash
/Users/atou/MiroFish/backend/.venv/bin/python /Users/atou/MiroFish/backend/scripts/run_world_model_eval_matrix.py \
  --base-config /Users/atou/MiroFish/backend/uploads/simulations/sim_8ac60f042d62/simulation_config.json \
  --python-bin /Users/atou/MiroFish/backend/.venv/bin/python
```

## Output Layout

Each run writes to `backend/uploads/evals/world-model-eval-<timestamp>/`.

- `eval_plan.json`
- `leaderboard.json`
- `summary.json`
- `summary.md`
- `report.md`
- `<case_id>/case_manifest.json`
- `<case_id>/metrics.json`
- `<case_id>/runs.json`
- `<case_id>/run-01/...`

Each run directory contains the isolated `simulation_config.json`, `run.log`, world artifacts, and per-run `metrics.json`.

The staged matrix runner writes to `backend/uploads/evals/world-model-matrix-<timestamp>/` and includes:

- `probe_results.json`
- `matrix_summary.json`
- `matrix_report.md`
- `stages/<stage_name>/...`
