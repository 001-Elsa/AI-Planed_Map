# MapGo-AI-Planner

[![CI](https://github.com/001-Elsa/AI-Planed_Map/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/001-Elsa/AI-Planed_Map/actions/workflows/ci.yml)

> 基于大模型意图理解、候选 POI 与路线联合求解、可验证约束和计划版本控制的智能出行平台。

MapGo 的定位不是“让模型生成一条看起来合理的路线”。系统把非确定性的需求理解与确定性的地图事实、约束求解和变更审批分开：

```text
自然语言
  → 类型化意图与澄清问题
    → Provider POI 候选召回
    → 带来源/质量/置信度的路线矩阵
    → 候选地点 + 访问顺序联合求解（精确枚举 / Beam / OR-Tools）
    → 硬约束验证 + 不确定约束安全缓冲
    → Plan Version
    → 待确认 Plan Patch
    → 伴游 Trip Session / Agent Controller / Worker 事件编排
```

核心边界：**LLM 可以提取约束和提出调整，但不能生成 POI、绕过验证器或直接覆盖正式计划。**

## 已实现能力（与代码同步）

- 唯一正式后端为 FastAPI；旧 Node API 已移除；
- SQLAlchemy 2.x 异步会话、PostgreSQL 16、显式 Alembic migration；
- Runtime Store 抽象（内存 / Redis）：计数、JSON 状态、队列、分布式锁、重试与死信、Pub/Sub；
- 后台 Worker：消费行程事件、通知去重投递、Agent Controller 编排、过期定位清理；
- LLM 运行时失败自动降级到 RuleBased 解析器，并写入不确定约束 / 降低置信度；
- 多轮规划澄清（起点、时间、人群、忌口、区域、候选 POI 选择等）；确认答案会写回类型化意图、重新召回候选并重新求解；
- 伴游 Agent：LLM 在受限 JSON 输出中根据 Trip State、Observation 和工具结果逐步决定下一工具；Policy、调用步数、Token/费用预算与完整 AgentRun/AgentToolCall 审计始终生效；
- 高影响事件经 Worker 的分布式锁触发 Agent 工具循环，自动生成 `pending` Plan Patch 并推送 SSE；正式 Version 只能由用户确认后创建；
- 动态重规划支持 `replace_stop`、`remove_stop`、`move_stop`、`change_transport_mode`；闭馆可找替代 POI，暴雨可将室外地点替为室内候选；
- 策展知识库 + 本地 TF-IDF RAG 检索与引文；拒绝无来源编造；
- 行程复盘摘要（站间偏差、重规划、建议接受/拒绝、ETA 误差）；
- 站内通知服务（去重、重试、投递状态；Web Push/邮件为可扩展通道）；
- JSON 日志、Request/Trace ID、Prometheus 指标（含 histogram 分桶）；
- CI：Ruff / Mypy / Bandit / pip-audit、Postgres+Redis、Alembic、pytest、AI 评测质量门禁、Playwright E2E、Docker build。

诚实边界：

- 置信度当前是基于 Provider 质量和安全缓冲的启发式区间，不是严格概率预报；代码中的历史残差校准函数尚未接入在线规划，因而不对外宣称已校准；
- RAG 为本地轻量检索，不是托管向量库；
- 通知的 Web Push / 邮件适配器尚未对接真实厂商；
- OpenTelemetry 全链路 SDK 仍可继续加深；当前以 Prometheus + Trace ID 为主。

## 目录

```text
MapGo/
├── backend/
│   ├── app/
│   │   ├── api/             # REST、伴游、规划、幂等与 Patch
│   │   ├── clients/         # Map / Weather / Knowledge
│   │   ├── infrastructure/  # Runtime Store（Redis/内存）
│   │   ├── knowledge/       # 策展景点知识
│   │   ├── services/        # 解析、规划、Agent、Worker 协作
│   │   ├── worker.py        # 后台事件 Worker
│   │   └── db/              # Session 与 Alembic migrations
│   └── tests/               # unit / integration / eval / load / chaos
├── public/                  # 地图 + AI Planner Web UI
├── docs/                    # 架构、ADR、安全与面试材料
├── test/e2e.run.cjs         # Playwright（uvicorn）
├── .github/workflows/       # CI
└── docker-compose.yml
```

## 本地运行

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt
copy .env.example .env
alembic upgrade head
python -m uvicorn backend.app.main:app --reload --port 3000
```

或：

```bash
docker compose up --build
```

Compose 启动 `migrate`、`api`、`worker`、`postgres`、`redis`；可选 `observability` profile 启动 Prometheus/Grafana。

## 常用命令

```bash
npm run migrate
npm test
npm run test:e2e
npm start
python backend/tests/evaluation/evaluate_intent.py
python backend/tests/chaos/run_chaos.py
python backend/tests/load/planning_load.py --token <token>
```

## 测试与证据

```bash
python -m pytest -c backend/pytest.ini
python backend/tests/evaluation/evaluate_intent.py
python backend/tests/chaos/run_chaos.py
```

AI 离线评测对 RuleBased 解析器施加质量门禁（Schema 合法率等）。压测与混沌脚本输出实测 JSON，不在文档中伪造 QPS。

更多设计见 [架构说明](docs/ARCHITECTURE.md)、[威胁模型](docs/THREAT_MODEL.md) 和 [ADR](docs/adr/0001-deterministic-planning-boundary.md)。

演示流程、可复现的测试与压测命令见 [演示与运行证据](docs/DEMO.md)。仓库展示名已调整为 **MapGo-AI-Planner**；GitHub 上的远端仓库重命名需在仓库设置中执行后，再同步更新 `origin`。

## 路线图（仍未完成 / 可继续加深）

- 真实 LLM 评测对比与 Prompt 回归；
- 托管向量库 / 重排序完整 RAG；
- Web Push / 邮件 / App Push 真实投递；
- OpenTelemetry 跨 API/Worker/DB/Redis 全链路；
- 录制演示视频 / GIF 并发布在线 Demo；
