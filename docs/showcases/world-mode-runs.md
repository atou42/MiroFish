# World Mode Showcase

本页把这条 fork 线已经真实跑过、真实复盘过的 world run 沉淀成可读 showcase，而不是只把运行产物留在本地 `backend/uploads/`。

## 阅读面与入口

| World Story Surface | World Report Entry |
|---|---|
| ![World story hero](./assets/world-story-hero.png) | ![World report entry](./assets/world-report-entry.png) |

- `world story` 页把真实运行产物压缩成适合外部阅读的五层结构：`hero / episodes / factions / risks / process`
- `report` 页右上角提供“世界故事页”入口，world case 不再需要靠手动 route 跳转
- 当前主 showcase case 可直接打开：`/world-story/sim_8ac60f042d62_fork384`

## Case 1: 分叉后长程 600 轮推进

- Simulation ID: `sim_8ac60f042d62_fork384`
- Diagnostics Label: `fork384_r600`
- 完成时间: `2026-03-23T22:55:44`
- 轮次: `600`
- Actions Log Rows: `9624`
- Actor Profile: `eval_aliyun_qwen35_flash`
- Resolver Profile: `eval_litellm_gpt54_deep`

### 关键统计

- Accepted Intents: `160`
- Deferred Intents: `560`
- Rejected Intents: `84`
- Resolver Salvaged: `0`
- Resolver Zero-Accept Diagnostics: `0`
- Accepted Event Sources:
  - `llm`: `156`
  - `llm_invalid_json_recovered`: `4`

### 终局世界状态

- Tension: `0.95`
- Stability: `0.075`
- Momentum: `0.95`

最后一轮摘要：

> 第 600 轮「航线咽喉与地方王国的连锁反应」结束：生成 4 个角色意图，其中 3 个被整理为事件，当前有 3 个活跃事件、6 个排队事件，世界紧张度 0.95，稳定度 0.07。

### 600 轮之后，世界已经推进到哪里

这条 case 从贝加庞克广播后的秩序震荡出发，到了 600 轮时，主冲突已经不再只是政府压制和海上骚动，而是灰石港一线围绕灰盾护航、闭门续谈、暗账结算、渗透取证和旧部联系窗口同时展开的竞争秩序。

比较能代表这条线后段质感的升级链是：

- 圣地保名单外泄
- 灰盾样本护航
- 闭门联合不干扰见证
- 单航次优先补给预约
- 样本合同最终结算与离岸安全屋转存
- CP-0 深入灰石港联络网取证
- 罗杰旧部联系窗口重新点亮

### 这个 case 说明了什么

- world runtime 已经可以长期维持高压态推进，同时保持 `0` 次 resolver salvage
- 长跑结果开始出现真正的制度竞争与秩序替代，而不只是单轮事件堆叠
- fork -> resume -> finalize -> diagnostics 这条分叉长跑链路已经被真实 case 证明

## Case 2: 长程 240 轮推进

- Simulation ID: `sim_8ac60f042d62`
- Diagnostics Label: `final240_autorun_clean`
- 完成时间: `2026-03-21T13:29:52`
- 轮次: `240`
- Actions Log Rows: `3594`
- Actor Profile: `eval_aliyun_qwen35_flash`
- Resolver Profile: `eval_litellm_gpt54_deep`

### 关键统计

- Accepted Intents: `142`
- Deferred Intents: `77`
- Rejected Intents: `28`
- Resolver Salvaged: `0`
- Resolver Zero-Accept Diagnostics: `0`
- Accepted Event Sources:
  - `llm`: `138`
  - `llm_invalid_json_recovered`: `4`

### 终局世界状态

- Tension: `0.95`
- Stability: `0.50`
- Momentum: `0.95`

最后一轮摘要：

> 第 240 轮「航线咽喉与地方王国的连锁反应」结束：生成 3 个角色意图，其中 2 个被整理为事件，当前有 1 个活跃事件、4 个排队事件，世界紧张度 0.95，稳定度 0.50。

### 这个 case 说明了什么

- world runtime 已经不是只能跑几轮的 demo，可以长时间推进
- checkpoint / diagnostics / report 链路在长跑后仍然可复盘
- `Qwen 3.5 Flash actor + GPT-5.4 deep resolver` 在这类 run 上能维持较高可解释性与较低 salvage 依赖

## Case 3: operator 状态桥接 smoke

- Simulation ID: `sim_world_supervised_smoke_20260321_172346`
- Diagnostics Label: `operator_bridge_smoke`
- 完成时间: `2026-03-21T17:28:55`
- 轮次: `2`
- Actions Log Rows: `20`
- Actor Profile: `eval_aliyun_qwen35_flash`
- Resolver Profile: `eval_litellm_gpt54_deep`

### 关键统计

- Accepted Intents: `2`
- Deferred Intents: `0`
- Rejected Intents: `0`
- Resolver Salvaged: `0`
- Intent Sources:
  - `llm`: `3`
  - `llm_invalid_json_partial_recovered`: `1`

### 终局世界状态

- Tension: `0.779`
- Stability: `0.215`
- Momentum: `0.762`

最后一轮摘要：

> 第 2 轮「海军高压执行与内部裂缝」结束：生成 2 个角色意图，其中 2 个被整理为事件，当前有 0 个活跃事件、2 个排队事件，世界紧张度 0.78，稳定度 0.21。

### 这个 case 说明了什么

- operator CLI 路径现在不再是“能跑但 UI 看不到”
- 运行中 `run_state.json` 会实时刷新
- 结束后 `run_state.json`、`checkpoint.json`、`world_run.py status` 会收敛到一致终态
- 对 operator 路径做 smoke 时，已经可以用这条链路验证真实 live state，而不是只盯 subprocess return code

## 当前推荐的默认实践

如果你要开始一轮新的 world run，建议按下面顺序做：

1. 先选 preset
2. 先做短 smoke，确认 provider 与 selector 健康
3. 再做 staged run，例如 `8 -> 16 -> 32`
4. 每个阶段都生成 diagnostics
5. 关键 case 再沉淀成 eval suite / case set

## 推荐 preset

| Preset | 说明 |
|--------|------|
| `recommended_throughput` | 默认吞吐优先 |
| `recommended_stable` | 更适合关键 run、解释性更强 |
| `smoke_benchmark_minimax` | 仅适合烟测，不建议直接当长期默认 |

参见：

- [../../backend/evals/world_runtime_presets.json](../../backend/evals/world_runtime_presets.json)
- [../../backend/evals/README.md](../../backend/evals/README.md)
- [../../backend/evals/world_model_eval_playbook.md](../../backend/evals/world_model_eval_playbook.md)
