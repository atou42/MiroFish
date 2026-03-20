# World Model Eval Playbook（基于 2026-03-19 这轮）

## 1) 目标

本文件用于把一次完整 world model eval 变成可复现流程，帮助后续使用者：
- 独立复跑同类评估（全量或分阶段）。
- 理解“为什么推荐这个策略”而不是只看最终分数。
- 把结论落到 `world_runtime_presets.json`，并区分“本轮结论”与“通用规则”。

本轮基线数据来自：
- campaign: `/Users/atou/MiroFish/backend/uploads/evals/world-model-campaign-20260319-122201`
- base config: `/Users/atou/MiroFish/backend/uploads/simulations/sim_8ac60f042d62/simulation_config.json`

---

## 2) 评估阶段（脚本真实流程）

主脚本：`/Users/atou/MiroFish/backend/scripts/run_world_model_eval_campaign.py`  
分阶段脚本：`/Users/atou/MiroFish/backend/scripts/run_world_model_eval_stage.py`

阶段顺序与目的：
1. `probe`
   - 做可用性 + 结构化输出探测（actor/resolver 各自 probe）。
   - 产物：`probe/availability.json`、`probe/actor_probe.json`、`probe/resolver_probe.json`、`probe_summary.json`。
2. `actor_smoke`（在实现里落盘为 `actor_smoke_dynamic`）
   - 固定 resolver baseline，比较 actor 候选。
   - 配置：`latency_smoke`，`max_rounds=1`，`repeat_count=1`。
3. `resolver_smoke`（落盘为 `resolver_smoke_dynamic`）
   - 固定 actor baseline，比较 resolver 候选。
   - 配置同上。
4. `pair_smoke`
   - 将 actor/resolver smoke 的前排组合交叉评测。
   - 配置同上。
5. `progression`
   - 对 pair finalists 跑 3 tick 迷你推进。
   - 配置：`progression_mini`，`max_rounds=3`，`repeat_count=1`。
6. `stability`
   - 对 progression finalists 做 repeat 评估稳定性。
   - 配置：`progression_mini`，`max_rounds=3`，`repeat_count=3`（本轮）。
7. `finalize`
   - 汇总输出 `campaign_summary.json` + `campaign_report.md`，并给出三类推荐（default/fastest/stable）。

---

## 3) 如何运行（全量 / 分阶段）

在 backend 目录执行（建议直接使用项目虚拟环境，避免系统 Python 依赖漂移）：

```bash
cd /Users/atou/MiroFish/backend
```

### 3.1 一次性全量跑

```bash
./.venv/bin/python scripts/run_world_model_eval_campaign.py \
  --base-config /Users/atou/MiroFish/backend/uploads/simulations/sim_8ac60f042d62/simulation_config.json \
  --campaign-config /Users/atou/MiroFish/backend/evals/world_model_eval_campaign.json \
  --output-root /Users/atou/MiroFish/backend/uploads/evals \
  --python-bin ./.venv/bin/python
```

### 3.2 复用既有 probe 再跑后续阶段

```bash
./.venv/bin/python scripts/run_world_model_eval_campaign.py \
  --base-config /Users/atou/MiroFish/backend/uploads/simulations/sim_8ac60f042d62/simulation_config.json \
  --campaign-config /Users/atou/MiroFish/backend/evals/world_model_eval_campaign.json \
  --output-root /Users/atou/MiroFish/backend/uploads/evals \
  --resume-probe-dir /Users/atou/MiroFish/backend/uploads/evals/world-model-campaign-20260319-122201/probe \
  --python-bin ./.venv/bin/python
```

### 3.3 分阶段跑（长任务推荐）

先建（或指定）`--campaign-dir`，然后按阶段顺序执行：

```bash
./.venv/bin/python scripts/run_world_model_eval_stage.py --stage probe          --base-config <base_config.json> --campaign-dir <campaign_dir> --python-bin ./.venv/bin/python
./.venv/bin/python scripts/run_world_model_eval_stage.py --stage actor_smoke    --base-config <base_config.json> --campaign-dir <campaign_dir> --python-bin ./.venv/bin/python
./.venv/bin/python scripts/run_world_model_eval_stage.py --stage resolver_smoke --base-config <base_config.json> --campaign-dir <campaign_dir> --python-bin ./.venv/bin/python
./.venv/bin/python scripts/run_world_model_eval_stage.py --stage pair_smoke     --base-config <base_config.json> --campaign-dir <campaign_dir> --python-bin ./.venv/bin/python
./.venv/bin/python scripts/run_world_model_eval_stage.py --stage progression    --base-config <base_config.json> --campaign-dir <campaign_dir> --python-bin ./.venv/bin/python
./.venv/bin/python scripts/run_world_model_eval_stage.py --stage stability      --base-config <base_config.json> --campaign-dir <campaign_dir> --python-bin ./.venv/bin/python
./.venv/bin/python scripts/run_world_model_eval_stage.py --stage finalize       --base-config <base_config.json> --campaign-dir <campaign_dir> --python-bin ./.venv/bin/python
```

---

## 4) 如何 resume

- 全量脚本仅支持 probe 复用：`--resume-probe-dir`。
- 分阶段脚本天然可 resume：
  - 已完成阶段的 summary 文件保留在 `<campaign_dir>` 根目录（例如 `progression_summary.json`）。
  - 从中断点后的阶段继续运行即可。
- 并发保护：
  - 分阶段脚本会创建 `.stage-lock-<stage>.json`，同一阶段正在跑时会拒绝第二个进程。
- 运行卫生：
  - 复跑前先确认没有遗留的旧 eval 进程继续写同一个 `campaign_dir`。这轮实际踩过“旧 worker 混进来，污染 latency / provider wait”的坑。
  - 如果怀疑结果异常漂移，优先新建一个全新的 `campaign_dir` 重跑，而不是在可疑目录上叠加。

---

## 5) 如何读取结果（从文件到结论）

优先看：
1. `campaign_summary.json`（机器可读总汇总，推荐作为“单一事实来源”）
2. `campaign_report.md`（面向人读的简版）
3. `decision_memo.md`（人工决策解释与风险边界）

关键字段解释（本轮实际用法）：
- `recommendations.default_strategy`
  - 默认推荐，来自 stability 阶段，要求 repeat 完成且综合表现最优。
- `recommendations.fastest_viable`
  - 在“可用池”（repeat 完成、events_completed>=1、salvage<=0.5）内按总时长/首事件时间等排序最优。
- `recommendations.most_stable_progression`
  - 优先看 resilience、跨 repeat 离散度（`score_spread.overall.std`）、salvage/provider_wait。

本轮对比核心（stability，均 completed_repeats=3/3）：
- `Qwen 3.5 Flash + Qwen 3.5 Plus`
  - `overall=72.4`（最高），`progression=75.6`（最高）
  - 代价：`salvage_tick_rate=0.444`，`provider_wait_total_s=1.333`，`overall std=15.568`
- `Qwen 3.5 Flash + GPT-5.4 deep`
  - `overall=72.1`（接近），`resilience=89.5`（更高）
  - 更快更干净：`simulation_total_s=407.583`，`first_event_s=153.797`，`provider_wait_total_s=0.0`，`salvage_tick_rate=0.222`，`overall std=13.078`

---

## 6) 本轮最终建议策略与 preset 对应关系（明确落地）

当前 preset 文件：`/Users/atou/MiroFish/backend/evals/world_runtime_presets.json`

推荐映射：
1. 默认吞吐（本轮主推荐）
   - 策略：`Qwen 3.5 Flash actor + Qwen 3.5 Plus resolver`
   - campaign selector：`campaign_aliyun_qwen35_flash + campaign_aliyun_qwen35_plus`
   - preset：`recommended_throughput`
   - preset selector：`eval_aliyun_qwen35_flash + eval_aliyun_qwen35_plus`
2. 稳定/快速可用
   - 策略：`Qwen 3.5 Flash actor + GPT-5.4 deep resolver`
   - campaign selector：`campaign_aliyun_qwen35_flash + campaign_litellm_gpt54_deep`
   - preset：`recommended_stable`
   - preset selector：`eval_aliyun_qwen35_flash + eval_litellm_gpt54_deep`
3. 烟测基准（非生产默认）
   - 策略：`Qwen 3.5 Flash actor + MiniMax resolver`
   - preset：`smoke_benchmark_minimax`
   - 仅用于 1 tick smoke/triage，不用于真实推进默认。

说明：campaign 使用临时 registry selector（`campaign_*`）；沉淀到 preset 时使用长期 selector（`eval_*`）。

---

## 7) 如何把结果沉淀到 preset

以 `world_runtime_presets.json` 为落点，按以下顺序做：
1. 用 `campaign_summary.json > recommendations` 确认要沉淀的策略组合与指标。
2. 更新/新增 preset 项：
   - `actor_selector`、`resolver_selector`
   - `runtime_overrides`（与本轮通过阶段一致）
   - `evaluation`（写入 campaign_date、stage、overall/progression/resilience/speed、events、provider_wait、salvage）
   - `notes`（写清 tradeoff，而非只写“最优”）
3. 设置 `default_preset`：
   - 若主目标是吞吐，指向 `recommended_throughput`（本轮已是如此）。
4. 保留一个 `registry_default` 作为回归基线，便于后续 A/B。

补充：如果这一轮结论已经稳定，不要只停留在 preset。
- 还要把长期要复跑的组合沉淀到 `backend/evals/world_case_sets/`。
- 推荐至少维护三类 case set：
  - `recommended_smoke`：快速回归。
  - `recommended_progression`：真实小规模推进回归。
  - `recommended_stability`：repeat 黄金集。
- 这样下次做 eval 时，不需要先回翻旧 campaign 目录找 finalists。

---

## 8) 这轮踩坑（供后续避坑）

1. Probe 排名会误导真实推进
   - 典型：`MiniMax resolver` probe/smoke 亮眼，但 progression/stability 的端到端表现掉队。
   - 更具体地说，smoke 阶段里“启动快”不代表“能完成事件”。本轮 `MiniMax resolver` 的 pair smoke 跑出 `overall=67.5`，但 `events_completed=0`。
2. 单阶段冠军不一定可迁移
   - actor probe 冠军是 `Qwen 3.5 Plus actor`，但最终迁移赢家是 `Qwen 3.5 Flash actor`。
3. 只看 overall 会掩盖运行风险
   - 需要同时看 `salvage_tick_rate`、`provider_wait_total_s`、`score_spread.overall.std`。
4. “最快可用”必须先过滤零完成样本
   - 这轮实际修过 `finalize` 口径：不能把 zero-completion smoke winner 直接当 fastest viable。
   - 现在脚本会先要求 `completed_repeats` 完成、`events_completed>=1`、`salvage_tick_rate<=0.5`，再比较速度。
5. 旧 eval worker 会污染稳定性判断
   - 如果后台还有遗留 case worker 往旧目录写结果，会把 `provider_wait_total_s`、`simulation_total_s`、甚至 repeat 汇总搞脏。
   - 看到“同一策略突然极端变慢/变快”时，先排查这个问题，不要急着换模型。
6. 供应商路由/协议问题会污染样本池
   - 本轮明确排除（见 `decision_memo.md`）：`codexvip/*`、`litellm-google/*`、`yunwu/*`、`zai/glm-5`、`bigmodel-pony/pony-alpha-2`、部分 claude-opus 路由。
7. 供应商失败策略会直接影响评估口径
   - 本项目当前 world runtime 倾向“无 fallback，等 provider 恢复或避开不可用窗口”，所以某些 provider 的短时抖动会真实反映到分数里。
   - 这符合本轮的运行哲学，但也意味着评估结果对当日 provider 可用性更敏感。

---

## 9) 适用边界（不要当成普适真理）

以下结论**仅适用于本轮环境与数据**：
- 时间窗口：2026-03-19 这次 campaign。
- world data：`sim_8ac60f042d62` 对应的剧情/状态初始化。
- 候选池：本轮 `world_model_eval_campaign.json` 中实际候选与强制入选规则。
- runtime 形状：smoke 1 tick、progression/stability 3 tick，且 stability repeat=3。
- provider 可用性：受当日路由与认证状态影响，可能随时间变化。

因此，“`Qwen Flash + Qwen Plus` 为默认”是**本轮最优决策**，不是跨数据集、跨供应商状态、跨时间窗口的永恒最优结论。
