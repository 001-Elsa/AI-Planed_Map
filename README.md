# MapGo AI-Planned

> 基于大模型意图理解、候选 POI 与路线联合求解、可验证约束和计划版本控制的智能出行平台。

MapGo 的定位不是“让模型生成一条看起来合理的路线”。系统把非确定性的需求理解与确定性的地图事实、约束求解和变更审批分开：

```text
自然语言
  → 类型化意图与澄清问题
  → Provider POI 候选召回
  → 带来源/质量/置信度的路线矩阵
  → 候选地点 + 访问顺序联合求解
  → 硬约束验证
  → Plan Version
  → 待确认 Plan Patch
  → 重算并再次验证后生成新版本
```

核心边界：**LLM 可以提取约束和提出调整，但不能生成 POI、绕过验证器或直接覆盖正式计划。**

## 已实现的企业级纵切面

- 唯一正式后端为 FastAPI；旧 Node API 已从主分支移除；
- SQLAlchemy 2.x 异步会话、PostgreSQL 16、显式 Alembic migration；
- 应用启动只检查连接和 schema revision，不执行 `create_all`；
- FastAPI lifespan 复用共享 `httpx.AsyncClient`、连接池和地图 Provider；
- 上游超时、有限重试、指数退避、并发隔离与 graceful shutdown；
- POI 与路线边记录 provider、数据时间、质量、置信度和 fallback；
- 多候选 POI 与路线顺序联合精确枚举，小规模爆炸时切换联合 Beam Search；
- 截止时间、最早到达、最低评分、任务顺序、总时长、步行上限和最晚返回等硬约束；
- 时间、步行、距离、评分与不确定性的版本化多目标代价函数；
- 类型化澄清问题和 `DRAFT → NEED_CLARIFICATION → PLAN_READY` 规划状态；
- 计划版本、待确认 Plan Patch、版本冲突检测、补丁后路线重算与硬约束复验；
- 决策审计记录 Policy 结果、理由、证据和 Trace ID；
- 幂等记录具备请求指纹、TTL 和 `processing/succeeded/failed/expired` 生命周期；
- JSON 请求日志、Request ID、Trace ID、模型/Prompt/Provider/耗时元数据；
- `/metrics` 暴露 API、规划、地图上游、失败和回退等 Prometheus 指标；
- CI 使用真实 PostgreSQL 执行 migration 升级/回滚、pytest、AI 离线评测、静态检查、安全扫描、E2E 和镜像构建。

尚未实现的能力会明确保留在路线图中，不在简历或 README 中伪装为完成：实时位置事件流、天气 Provider、后台 Worker、伴游 Agent Controller、通知和景点 RAG。

## 目录

```text
MapGo/
├── backend/
│   ├── app/
│   │   ├── api/          # REST、幂等、版本与 Patch 审批
│   │   ├── clients/      # Map Provider
│   │   ├── schemas/      # 意图、约束、来源与 Patch Schema
│   │   ├── services/     # 解析、规划编排、联合求解
│   │   └── db/           # Session 与 Alembic migrations
│   └── tests/
├── public/               # 地图 + AI Planner Web UI
├── docs/                 # 架构、ADR、安全与面试材料
├── .github/workflows/    # 生产链路 CI
├── docker-compose.yml
└── Makefile
```

## 本地运行

Python 3.12：

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r backend/requirements.txt
copy .env.example .env
alembic upgrade head
python -m uvicorn backend.app.main:app --reload --port 3000
```

或使用 PostgreSQL：

```bash
docker compose up --build
```

Compose 先运行一次性 `migrate` 服务；只有迁移成功后才启动只读文件系统、非 root 的 API 容器。打开 <http://localhost:3000>，OpenAPI 位于 <http://localhost:3000/docs>。

未设置 `AMAP_WEB_KEY` 时后端使用确定性 Mock Provider。Mock 和球面距离结果始终标记为 `estimated`，不会包装成实时地图事实。

## 常用命令

```bash
make dev
make migrate
make test
make lint
make eval
make compose
```

Windows 未安装 `make` 时可使用：

```bash
npm run migrate
npm test
npm start
```

## AI 规划接口

```http
POST /api/ai/plans
Authorization: Bearer <token>
Idempotency-Key: <uuid>
Content-Type: application/json
```

```json
{
  "text": "明天下午两点从学校出发，先取快递，再买水果，五点前到医院",
  "origin": {"lng": 116.397, "lat": 39.908},
  "transport_mode": "walking",
  "max_candidates_per_task": 3,
  "constraints": {
    "hard": {
      "max_walking_meters": 4500,
      "must_return_to_origin": false,
      "required_task_order": []
    },
    "uncertain": []
  }
}
```

关键返回字段：

```json
{
  "status": "success",
  "planning_state": "PLAN_READY",
  "algorithm": "joint-exact-enumeration",
  "confidence": 0.65,
  "candidate_count": 9,
  "plan_version": 1,
  "stops": [{
    "travel": {
      "source": "amap_distance_v3",
      "quality": "provider",
      "confidence": 0.9,
      "fallback_used": false
    }
  }]
}
```

## Plan Patch

重要变更不会直接执行：

```http
POST /api/ai/plans/{run_id}/patches
POST /api/ai/plans/{run_id}/patches/{patch_id}/decision
GET  /api/ai/plans/{run_id}/versions
```

Patch 创建后为 `pending`。用户接受后，服务重新获取路线矩阵、重算 ETA、复验硬约束；不可行则返回 `PATCH_INFEASIBLE`，原版本保持不变。

## 测试与证据

```bash
python -m pytest -c backend/pytest.ini
python backend/tests/evaluation/evaluate_intent.py
alembic upgrade head
alembic downgrade 0001
alembic upgrade head
```

CI 中还执行 Ruff、Mypy、Bandit、pip-audit、覆盖率、PostgreSQL migration consistency、Playwright 和 Docker build。离线评测输出由提交的用例实时计算；项目不在文档中手写或伪造成功率。

更多设计见 [架构说明](docs/ARCHITECTURE.md)、[威胁模型](docs/THREAT_MODEL.md) 和 [ADR](docs/adr/0001-deterministic-planning-boundary.md)。

## 路线图

- P1：扩大约束评测集、加入 OR-Tools CP-SAT 基准实现、预算/费用 Provider；
- P2：Trip Session、事件去重、Agent Controller、Policy Engine、动态局部重规划；
- P3：Redis/Worker、OpenTelemetry/Prometheus、故障注入、压测报告、在线演示。

最终目标：

> **MapGo AI-Planned：基于大模型意图理解、约束路线求解与动态伴游 Agent 的智能出行平台。**
