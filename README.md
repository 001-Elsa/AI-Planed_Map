# MapGo-AI-Planner

[![CI](https://github.com/001-Elsa/AI-Planed_Map/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/001-Elsa/AI-Planed_Map/actions/workflows/ci.yml)

> 基于大模型意图理解、候选 POI 与路线联合求解、可验证约束和计划版本控制的智能出行平台。

MapGo 的定位不是“让模型生成一条看起来合理的路线”。系统把非确定性的需求理解与确定性的地图事实、约束求解和变更审批分开：

```text
自然语言
  → 类型化意图与澄清问题
    → Provider POI 候选召回
    → 带来源/质量/置信度的路线矩阵
    → 候选地点 + 访问顺序联合求解（精确枚举 / OR-Tools / Beam 回退）
    → Critic Agent 证据/偏好审阅（shadow 或 enforce）
    → 硬约束验证 + 不确定约束安全缓冲
    → Plan Version
    → 待确认 Plan Patch
    → 伴游 Trip Session / Agent Controller / Worker 事件编排
```

核心边界：**LLM 可以提取约束和提出调整，但不能生成 POI、绕过验证器或直接覆盖正式计划。**

## 已实现能力（与代码同步）

- 唯一正式后端为 FastAPI；旧 Node API 已移除；
- SQLAlchemy 2.x 异步会话与显式 Alembic migration；Compose/CI 使用 PostgreSQL 16，本地开发与 E2E 可使用 SQLite；
- Runtime Store 抽象（内存 / Redis）：计数、JSON 状态、队列、分布式锁、延迟重试、死信与事件发布；内存实现限制缓存条目、总字节数和计数器数量，计数器 key 洪泛时失败关闭；
- 后台 Worker：消费行程事件、通知去重投递、Agent Controller 编排、过期定位清理；
- LLM 运行时失败自动降级到 RuleBased 解析器，并写入不确定约束 / 降低置信度；
- 多轮规划澄清（起点、时间、人群、忌口、区域、候选 POI 选择等）；确认答案会写回类型化意图、重新召回候选并重新求解；
- Supervisor 拓扑：Supervisor Agent 负责任务拆分、调度、状态管理和错误恢复；Intent/Search/Planner/Critic 形成规划链路，Final Answer 由 Supervisor 收束；Companion Agent 继续作为行中事件处理角色。Search/Planner 是受控确定性阶段，不给 LLM 开放地图或求解工具权限；
- Agent 通信协议 v1：所有规划交接和 Companion 工具循环使用统一结构化消息信封，包含 sender/receiver、task/correlation/causation ID、消息与 Artifact 类型、内容哈希和幂等键；Router 对允许路由执行失败关闭校验，审计内容先最小化再持久化；
- Shared State v1：Redis/内存 RuntimeStore 保存带 revision 和 TTL 的当前任务状态，统一包含用户需求、POI 候选、路线方案、Critic 评价和执行历史；Agent 只能读取角色切片、写入本角色字段，更新使用 CAS 防止并发覆盖；PostgreSQL 只保存最小化状态快照、明确确认的长期偏好和正式任务/行程历史；
- Agent Tool Registry：角色授权、调用模式与数据域统一失败关闭校验；Intent/Search/Planner 的解析、地图和 OR-Tools 能力只允许服务器内部阶段调用，不进入 LLM Tool Schema；Companion 只暴露四个行中工具，并继续叠加 Trip State、Consent、确认与预算 Policy；
- Agent Memory：短期规划上下文保存在 Redis/RuntimeStore，任务终止时主动删除、TTL 仅兜底；长期偏好只接受用户明确确认并写 PostgreSQL，本次请求优先，可单次关闭、逐项撤销、导出或整库清除；Agent 本身没有用户偏好数据库访问权；
- Agent Evaluation：版本化意图与路线 golden cases 进入 CI；路线按距离 40%、时间 30%、偏好 30% 确定性评分，截止时间、重复 POI、硬约束和极端距离一票否决；同一评分器由运行时 Critic 复用并写入 Review Report；
- `off | shadow | enforce` 灰度模式、工作流总成本/交接上限、角色级 Token/延迟/回退审计与 Prometheus 指标；
- 伴游 Agent：LLM 在受限 JSON 输出中根据 Trip State、Observation 和工具结果逐步决定下一工具；Policy、调用步数、Token/费用预算与完整 AgentRun/AgentToolCall 审计始终生效；
- 高影响事件可经 Worker 的行程级分布式锁触发 Agent 工具循环，生成 `pending` Plan Patch，并通过最新状态 SSE 快照通知前端；正式 Version 只能由用户确认后创建；
- 动态重规划支持 `replace_stop`、`remove_stop`、`move_stop`、`change_transport_mode`；闭馆可找替代 POI，暴雨可将室外地点替为室内候选；
- 策展知识库 + 本地 TF-IDF RAG 检索与引文；拒绝无来源编造；
- 行程复盘摘要（站间偏差、重规划、建议接受/拒绝、ETA 误差）；
- 站内通知服务（去重、重试、投递状态；Web Push/邮件为可扩展通道）；
- JSON 日志、经过白名单清洗的 Request/Trace ID、低基数 Prometheus 指标（含 histogram 分桶），以及 API `no-store`、Permissions Policy 等安全响应头；
- CI：Ruff / Mypy / Bandit / pip-audit、Postgres+Redis、Alembic、pytest、AI 评测质量门禁、Playwright E2E、Docker build。

诚实边界：

- 置信度当前是基于 Provider 质量和安全缓冲的启发式区间，不是严格概率预报；代码中的历史残差校准函数尚未接入在线规划，因而不对外宣称已校准；
- RAG 为本地轻量检索，不是托管向量库；
- 通知的 Web Push / 邮件适配器尚未对接真实厂商；
- OpenTelemetry 全链路 SDK 仍可继续加深；当前以 Prometheus + Trace ID 为主。
- Patch 接受阶段复用联合求解评价器，重新验证任务完整性/顺序、时间、评分、营业、无障碍、区域、步行、时长和总费用；分类预算与绕行基线仍受 Provider 数据完整性限制；
- Worker 使用 processing 保留队列、ack、启动恢复、应用层重试和 DLQ；成功提交后未 ack 可能造成重复投递，因此依赖事件幂等，不宣称 Exactly Once；
- SSE 保存并推送最新行程状态快照，`Last-Event-ID` 用于避免重复展示该快照，不提供逐条、无缺口的历史事件回放；
- Exact / OR-Tools 求解已通过线程与 API 事件循环隔离；大规模 CPU 密集请求仍可进一步隔离到进程池或独立规划任务。

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
# PowerShell: .venv\Scripts\Activate.ps1
# cmd.exe: .venv\Scripts\activate.bat
pip install -r backend/requirements.txt
copy .env.example .env
alembic upgrade head
python -m uvicorn backend.app.main:app --reload --port 3000
```

或：

```bash
docker compose up --build
```

Compose 默认按生产环境启动。运行前必须把 `.env` 中的 `POSTGRES_PASSWORD`、
`GRAFANA_ADMIN_PASSWORD`、`ADMIN_INIT_TOKEN` 和 `LOCATION_ENCRYPTION_KEY`
替换为独立的强随机值；占位值或空值会使服务拒绝启动。普通用户注册不需要
`ADMIN_INIT_TOKEN`；只有显式选择管理员账号的注册和登录才必须提交该令牌，系统不会按“首个注册者”自动授予管理员权限。

当前注册密码长度为 8～64 个字符。登录/注册按来源 IP 执行独立限流，不信任可由客户端任意更换的设备 Header；已登录 API 另按 Session Token 限流。公开分享链接使用 128 bit（32 位 hex）随机 capability token，旧 16 位链接继续兼容读取。

Compose 启动 `migrate`、`api`、`worker`、`postgres`、`redis`；可选 `observability` profile 启动 Prometheus/Grafana。

## 常用命令

```bash
npm run migrate
npm test
npm run test:e2e
npm start
python backend/tests/evaluation/evaluate_intent.py
python backend/tests/evaluation/evaluate_agents.py
python backend/tests/evaluation/evaluate_routes.py
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

更多设计见 [架构说明](docs/ARCHITECTURE.md)、[威胁模型](docs/THREAT_MODEL.md)、[版本演进](docs/CHANGELOG.md) 和 [ADR](docs/adr/0001-deterministic-planning-boundary.md)。

演示流程、可复现的测试与压测命令见 [演示与运行证据](docs/DEMO.md)。仓库展示名已调整为 **MapGo-AI-Planner**；GitHub 上的远端仓库重命名需在仓库设置中执行后，再同步更新 `origin`。

## 路线图（仍未完成 / 可继续加深）

- 真实 LLM 评测对比与 Prompt 回归；
- 托管向量库 / 重排序完整 RAG；
- Web Push / 邮件 / App Push 真实投递；
- OpenTelemetry 跨 API/Worker/DB/Redis 全链路；
- 录制演示视频 / GIF 并发布在线 Demo；
