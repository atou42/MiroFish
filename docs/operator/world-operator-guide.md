# World Operator Guide

这个 guide 只讲一条主线：把原始世界资料编译成可运行 simulation，然后用统一的 operator CLI 做 smoke、长跑、诊断和复盘。

## World Pack Compilation

推荐先把原始资料编译成 world pack，而不是手动拼 `simulation_config.json`。

```bash
cd backend

./.venv/bin/python scripts/world_run.py compile-pack \
  --source-dir /absolute/path/to/world-materials \
  --simulation-id sim_my_world \
  --pack-title "My World" \
  --no-llm-profiles
```

默认行为：

- 会在 `backend/uploads/simulations/<simulation_id>/` 下直接生成可运行目录
- 默认使用 deterministic config，不会额外调用 config LLM
- profile 侧可以通过去掉 `--no-llm-profiles` 打开 LLM 人设增强

关键产物：

- `simulation_config.json`
- `world_profiles.json`
- `world_pack/manifest.json`
- `world_pack/sources.json`
- `world_pack/source_digest.md`
- `world_pack/compiled_entities.json`
- `state.json`

建议：

- 原始资料里如果已经有 `mirofish_import/*.md`、角色卡或结构化 `characters.json`，优先直接喂给 compiler
- `compile-pack` 是“洗资料 + bootstrap simulation”，不是一次正式 run
- 如果你确实想让 config 也走一次 LLM enrich，可以额外加 `--use-llm-config`

## Long-Run Pipeline

最小 smoke：

```bash
./.venv/bin/python scripts/world_run.py run \
  --simulation-id sim_my_world \
  --max-rounds 2
```

推荐的监督式长跑：

```bash
./.venv/bin/python scripts/world_run.py pipeline \
  --simulation-id sim_my_world \
  --stage-rounds 8,16,32,64
```

这个 pipeline 会在每个阶段自动做：

- `run` 或 `resume`
- diagnostics 汇总
- reading surfaces 生成
- report 生成
- manifest 落盘到 `world/pipeline_runs/<timestamp>/manifest.json`

常用命令：

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

实践建议：

1. 先 `compile-pack`
2. 先跑 `2` 轮 smoke
3. 再用 `pipeline` 做 `8 -> 16 -> 32 -> ...`
4. 如果要复盘某一段，不必整段重跑，直接看对应 stage 的 diagnostics / report / chronicle

## Reading Surfaces

`world_run_diagnostics.py` 现在除了传统统计，还会自动生成三类阅读面。

### World Chronicle

来源：

- `world/state_snapshots.jsonl`
- `world/actions.jsonl` 的 `tick_end`

用途：

- 看每个 tick 的主线发生了什么
- 看 accepted / completed / opened threads 是怎么串起来的

单独重生成：

```bash
./.venv/bin/python scripts/generate_world_reading_surface.py \
  --simulation-id sim_my_world \
  --label final64
```

### Faction / Actor State Board

来源：

- `world/checkpoint.json` 的 `actor_selection_counts` / `actor_event_counts`
- `simulation_config.json` 的 `agent_configs`

用途：

- 看哪些 actor 最活跃
- 看谁手里还有 active / queued threads
- 看某次 run 到底是谁在主导世界推进

### Unresolved Risk Digest

来源：

- 优先 `simulation_end.unresolved_events`
- 没有时回退到 `checkpoint.json` 的 `active_events + queued_events`
- 再叠加 `world_state.pressure_tracks`

用途：

- 看这次 run 停下来时还有哪些高优先级事件没收束
- 看风险是“具体未决事件”还是“宏观压力还没降下来”

默认输出位置：

- `backend/uploads/simulations/<simulation_id>/world/diagnostics/`
