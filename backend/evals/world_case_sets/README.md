# World Case Sets

This directory promotes one-off campaign outputs into durable, reusable `suite.json`-compatible case packs.

## Principles

- Each file here is directly consumable by `backend/scripts/eval_world_models.py`.
- Baseline cases use stable route selectors like `world_agent` / `world_resolver`.
- Promoted campaign cases use stable selector ids like `eval_aliyun_qwen35_flash`, not temporary `campaign_*` selectors.
- Metadata such as `source_case_id`, `preset_id`, and `source_metrics` is kept inline for lineage. The evaluator ignores unknown fields, so the files stay runnable.

## Included Sets

- `recommended_smoke.json`
  - Quick 1-tick regression for the baseline, the two recommended presets, and the MiniMax smoke canary.
- `recommended_progression.json`
  - 3-tick regression for the same promoted strategies.
- `recommended_stability.json`
  - 3-repeat golden suite for the durable finalists plus the registry baseline.
- `campaign_20260319_pair_smoke_top9.json`
  - Promoted copy of the full pair-smoke top-actor x top-resolver grid from the 2026-03-19 campaign.
- `campaign_20260319_stability_goldens.json`
  - Promoted copy of the two stability finalists from the same campaign.

## List / Materialize

List sets:

```bash
/Users/atou/MiroFish/backend/.venv/bin/python /Users/atou/MiroFish/backend/scripts/build_world_eval_suite_from_case_set.py --list
```

Print a selected set to stdout:

```bash
/Users/atou/MiroFish/backend/.venv/bin/python /Users/atou/MiroFish/backend/scripts/build_world_eval_suite_from_case_set.py \
  --case-set-id recommended_progression \
  --stdout
```

Materialize a filtered stability suite:

```bash
/Users/atou/MiroFish/backend/.venv/bin/python /Users/atou/MiroFish/backend/scripts/build_world_eval_suite_from_case_set.py \
  --case-set-id recommended_stability \
  --case-ids recommended-throughput,recommended-stable \
  --repeat-count 5 \
  --output /tmp/world_recommended_stability_repeat5.json
```

## Run

You can point the evaluator directly at any file in this directory:

```bash
/Users/atou/MiroFish/backend/.venv/bin/python /Users/atou/MiroFish/backend/scripts/eval_world_models.py \
  --base-config /Users/atou/MiroFish/backend/uploads/simulations/sim_8ac60f042d62/simulation_config.json \
  --suite-config /Users/atou/MiroFish/backend/evals/world_case_sets/recommended_progression.json \
  --python-bin /Users/atou/MiroFish/backend/.venv/bin/python
```
