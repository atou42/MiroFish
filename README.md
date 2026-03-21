<div align="center">

<img src="./static/image/MiroFish_logo_compressed.jpeg" alt="MiroFish Logo" width="72%"/>

# MiroFish World Fork

面向长程 world simulation 的 MiroFish 分支：多模型路由、checkpoint / resume、preset / eval、operator 工具链，以及可持续推进的世界演绎工作流。

</div>

## Fork 说明

- 上游项目：[`666ghj/MiroFish`](https://github.com/666ghj/MiroFish)
- 本仓库定位：保留 GitHub fork 关系，在上游能力基础上，把 `world mode` 打磨成更适合长期运行、持续观测、持续迭代的版本
- 协议延续：`AGPL-3.0`

如果你想看上游项目原始的“通用预测引擎 / 社交模拟”定位，请直接参考 upstream。  
如果你要的是“给定世界观、角色、冲突与规则，让系统稳定地长期推进，并能 checkpoint / resume / eval / diagnostics”，这个 fork 才是现在更合适的入口。

## 这个 fork 重点做了什么

- 把 `world mode` 当成一等公民，而不是附属功能
- 增强 `checkpoint -> resume -> restore -> finalize` 的完整链路
- 增强 operator CLI：
  - `run`
  - `resume`
  - `status`
  - `restore`
  - `finalize`
  - `staged`
- 打通运行态观测：
  - `run_state.json`
  - `world/checkpoint.json`
  - `world/actions.jsonl`
  - `world/state_snapshots.jsonl`
  - `world/diagnostics/*.md|json`
- 支持多 provider / 多 model 路由：
  - `llm_registry.json`
  - OpenClaw 配置复用
  - world actor 级别 `llm_selector`
- 增强 world preset 与 eval 体系：
  - `backend/evals/world_runtime_presets.json`
  - `backend/evals/world_model_eval_*.json`
  - `backend/evals/world_case_sets/*.json`

## 适合什么场景

- 世界观推进
- 长篇剧情沙盘
- 群像角色博弈
- 架空政治 / 地缘 / 舆情推演
- 有状态、可续跑、可复盘的长期模拟

## Showcase

详细案例见：[docs/showcases/world-mode-runs.md](./docs/showcases/world-mode-runs.md)

当前这个 fork 已经沉淀了两类代表性案例：

| Case | 日期 | 规模 | 结果 |
|------|------|------|------|
| `sim_8ac60f042d62` | 2026-03-21 | 240 ticks | 长程 world run 完成，`3594` 条 action log，`142` 个 accepted intents，`0` 次 resolver salvage |
| `sim_world_supervised_smoke_20260321_172346` | 2026-03-21 | 2 ticks | operator 路径 smoke 完成，`run_state / checkpoint / status` 三链路一致 |

这两个 showcase 的意义不同：

- `sim_8ac60f042d62` 证明世界可以长时间推进，而不是只能做一次性 demo
- `sim_world_supervised_smoke_20260321_172346` 证明 operator 路径不是“能跑但不可观测”，而是中途可看、终态可对账、诊断可导出

## 当前推荐策略

这轮 eval 之后，建议把 world runtime 默认分成两类策略：

| Preset | Actor | Resolver | 适用场景 |
|--------|-------|----------|----------|
| `recommended_throughput` | `eval_aliyun_qwen35_flash` | `eval_aliyun_qwen35_plus` | 默认吞吐优先，适合常规推进 |
| `recommended_stable` | `eval_aliyun_qwen35_flash` | `eval_litellm_gpt54_deep` | 稳定 / 可解释性优先，适合关键 run |
| `smoke_benchmark_minimax` | `eval_aliyun_qwen35_flash` | MiniMax smoke resolver | 仅适合烟测与 triage，不建议直接做长期默认 |

对应文件：

- [backend/evals/world_runtime_presets.json](./backend/evals/world_runtime_presets.json)
- [backend/evals/README.md](./backend/evals/README.md)
- [backend/evals/world_model_eval_playbook.md](./backend/evals/world_model_eval_playbook.md)

## 快速开始

### 1. 环境要求

| 工具 | 版本要求 |
|------|----------|
| Node.js | `18+` |
| Python | `>=3.11` |
| uv | 最新版 |

### 2. 配置环境变量

```bash
cp .env.example .env
cp llm_registry.json.example llm_registry.json
```

建议优先使用 OpenClaw + `llm_registry.json` 作为 provider / model 的统一管理方式。

最小配置建议：

```env
LLM_REGISTRY_SOURCE=auto
LLM_REGISTRY_PATH=/absolute/path/to/MiroFish/llm_registry.json
OPENCLAW_CONFIG_PATH=/Users/<you>/.openclaw/openclaw.json

# 当前后端默认依赖 Zep Cloud
ZEP_API_KEY=your_zep_api_key

# legacy fallback，仅在 registry / OpenClaw 都未命中时回退
LLM_API_KEY=
LLM_BASE_URL=
LLM_MODEL_NAME=
```

说明：

- `ZEP_API_KEY` 目前仍是这个项目运行链路的一部分
- `llm_registry.json` 支持多 provider、多 profile、多 route
- world actor 可以通过 `llm_selector` 做单 agent 级别选模
- 可以直接复用 OpenClaw 的模型配置，而不是把所有 model 写死在 `.env`

### 3. 安装依赖

```bash
npm run setup:all
```

如果你只关心 backend / CLI world run：

```bash
npm run setup:backend
```

### 4. 启动前后端

```bash
npm run dev
```

默认地址：

- 前端：`http://localhost:3000`
- 后端：`http://localhost:5001`

### 5. 从 UI 准备 world simulation

基本流程：

1. 上传世界观资料 / 角色卡 / 剧情资料
2. 选择 `World Mode`
3. 让系统生成 `simulation_config.json`
4. 通过 UI 直接运行，或切换到 CLI/operator 路径继续控制

## World CLI / Operator 工作流

如果你已经有一个准备好的 `simulation_config.json`，可以直接用 operator CLI 跑：

```bash
cd backend

./.venv/bin/python scripts/world_run.py run \
  --config /absolute/path/to/simulation_config.json \
  --max-rounds 8
```

续跑：

```bash
./.venv/bin/python scripts/world_run.py resume \
  --config /absolute/path/to/simulation_config.json \
  --max-rounds 16
```

查看状态：

```bash
./.venv/bin/python scripts/world_run.py status \
  --config /absolute/path/to/simulation_config.json
```

生成诊断：

```bash
./.venv/bin/python scripts/world_run_diagnostics.py \
  --simulation-id <simulation_id> \
  --label run16
```

生成报告：

```bash
./.venv/bin/python scripts/world_run.py finalize \
  --simulation-id <simulation_id> \
  --label final16
```

8 -> 16 的 staged run：

```bash
./.venv/bin/python scripts/world_run.py staged \
  --simulation-id <simulation_id> \
  --stage1-rounds 8 \
  --final-rounds 16
```

### 运行态观测的一个重要细节

这个 fork 里：

- `run_state.json` 用来表达“服务/UI 侧看到的 live state”
- `checkpoint.json` 用来表达“最后一个已提交 tick 的权威落点”

所以在 tick 正在执行时，`run_state` 可能会比 `checkpoint` 更靠前。  
这不是 bug，而是刻意设计：

- `checkpoint` 保持提交边界清晰
- `run_state` 保持 operator / UI 的过程可观测

终态完成后，两者会被自动对齐。

## 模型路由与配置

这个 fork 不推荐只靠单组 `LLM_API_KEY / LLM_MODEL_NAME` 暴力驱动全部 agent。

推荐方式：

1. 用 `llm_registry.json` 维护 providers / profiles / routes
2. 用 OpenClaw 作为模型配置的统一来源
3. world runtime 通过 preset 和 selector 做职责分层

你可以从这里开始看：

- [llm_registry.json.example](./llm_registry.json.example)
- [backend/evals/world_runtime_presets.json](./backend/evals/world_runtime_presets.json)
- [backend/app/services/world_preset_registry.py](./backend/app/services/world_preset_registry.py)

## Eval 体系

这个 fork 不是“凭感觉换模型”，而是尽量把 world strategy 评估沉淀成可复跑资产。

相关入口：

- [backend/evals/README.md](./backend/evals/README.md)
- [backend/evals/world_model_eval_playbook.md](./backend/evals/world_model_eval_playbook.md)
- [backend/evals/world_case_sets/README.md](./backend/evals/world_case_sets/README.md)

核心脚本：

- [backend/scripts/eval_world_models.py](./backend/scripts/eval_world_models.py)
- [backend/scripts/run_world_model_eval_campaign.py](./backend/scripts/run_world_model_eval_campaign.py)
- [backend/scripts/run_world_model_eval_stage.py](./backend/scripts/run_world_model_eval_stage.py)
- [backend/scripts/eval_world_strategy.py](./backend/scripts/eval_world_strategy.py)

## 仓库结构

```text
MiroFish/
├── backend/
│   ├── app/
│   ├── evals/
│   ├── scripts/
│   └── uploads/
├── frontend/
├── docs/
│   └── showcases/
├── llm_registry.json.example
├── README.md
└── README-EN.md
```

你最常会用到的目录：

- `backend/scripts/`：world run / diagnostics / report / eval 脚本
- `backend/evals/`：preset、eval suite、playbook、case sets
- `backend/uploads/simulations/`：实际运行产物
- `docs/showcases/`：可直接放到 GitHub 上看的 showcase 文档

## 当前已知边界

- world mode 的深度互动仍以 `ReportAgent` 为主，而不是实时角色采访
- tick 级 checkpoint 只在提交点写盘，因此中途态主要看 `run_state.json`
- world runtime 的质量仍显著受 provider 可用性与模型结构化输出能力影响
- 这个 fork 已经把“为什么出问题、问题落在哪、如何复跑定位”做成了 diagnostics / eval / playbook，但它仍不是零运维成本系统

## Upstream 与致谢

- 上游项目：[`666ghj/MiroFish`](https://github.com/666ghj/MiroFish)
- 原始仿真引擎：[`camel-ai/oasis`](https://github.com/camel-ai/oasis)

感谢 upstream 作者把基础框架开源出来。  
这个 fork 的目标不是抹掉 upstream，而是在保留关系与协议的前提下，把 world simulation 这条线继续向前推进。

## License

本仓库沿用上游协议：`AGPL-3.0`
