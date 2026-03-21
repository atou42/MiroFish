# World Operator Guide

This guide covers one practical path: compile raw world materials into a runnable simulation, then use the operator CLI for smoke runs, long runs, diagnostics, and review.

## World Pack Compilation

Prefer compiling a world pack instead of hand-authoring `simulation_config.json`.

```bash
cd backend

./.venv/bin/python scripts/world_run.py compile-pack \
  --source-dir /absolute/path/to/world-materials \
  --simulation-id sim_my_world \
  --pack-title "My World" \
  --no-llm-profiles
```

Default behavior:

- writes a runnable simulation directory into `backend/uploads/simulations/<simulation_id>/`
- uses deterministic config generation by default
- profile generation can be upgraded with LLMs by removing `--no-llm-profiles`

Key artifacts:

- `simulation_config.json`
- `world_profiles.json`
- `world_pack/manifest.json`
- `world_pack/sources.json`
- `world_pack/source_digest.md`
- `world_pack/compiled_entities.json`
- `state.json`

Guidance:

- if your materials already contain `mirofish_import/*.md`, character cards, or structured `characters.json`, feed those directly into the compiler
- `compile-pack` is the normalization + bootstrap step, not the actual run
- if you explicitly want config enrichment through an LLM, add `--use-llm-config`

## Long-Run Pipeline

Minimal smoke:

```bash
./.venv/bin/python scripts/world_run.py run \
  --simulation-id sim_my_world \
  --max-rounds 2
```

Recommended supervised long run:

```bash
./.venv/bin/python scripts/world_run.py pipeline \
  --simulation-id sim_my_world \
  --stage-rounds 8,16,32,64
```

At each stage, the pipeline automatically does:

- `run` or `resume`
- diagnostics generation
- reading surface generation
- report generation
- manifest persistence into `world/pipeline_runs/<timestamp>/manifest.json`

Common commands:

```bash
./.venv/bin/python scripts/world_run.py status \
  --simulation-id sim_my_world
```

```bash
./.venv/bin/python scripts/world_run.py finalize \
  --simulation-id sim_my_world \
  --label final64
```

```bash
./.venv/bin/python scripts/world_run.py staged \
  --simulation-id sim_my_world \
  --stage1-rounds 8 \
  --final-rounds 16
```

Practical sequence:

1. `compile-pack`
2. run a `2`-tick smoke
3. use `pipeline` for `8 -> 16 -> 32 -> ...`
4. review each stage through diagnostics / report / chronicle instead of rerunning blindly

## Reading Surfaces

`world_run_diagnostics.py` now generates three higher-level reading surfaces in addition to the traditional counters.

### World Chronicle

Sources:

- `world/state_snapshots.jsonl`
- `tick_end` rows in `world/actions.jsonl`

Use it to:

- read the world as a tick-by-tick trajectory
- understand how accepted, completed, and newly opened threads chain together

Regenerate directly:

```bash
./.venv/bin/python scripts/generate_world_reading_surface.py \
  --simulation-id sim_my_world \
  --label final64
```

### Faction / Actor State Board

Sources:

- `actor_selection_counts` / `actor_event_counts` in `world/checkpoint.json`
- `agent_configs` in `simulation_config.json`

Use it to:

- see which actors were most active
- inspect which factions still hold active or queued threads
- identify who is really driving the world forward in a given run

### Unresolved Risk Digest

Sources:

- first choice: `simulation_end.unresolved_events`
- fallback: `active_events + queued_events` in `checkpoint.json`
- plus the current `world_state.pressure_tracks`

Use it to:

- see which high-priority events remain unresolved when the run stops
- distinguish between concrete pending incidents and unresolved macro pressure

Default output directory:

- `backend/uploads/simulations/<simulation_id>/world/diagnostics/`
