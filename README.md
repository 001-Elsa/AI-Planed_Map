# MapGo AI Planner

> 基于大模型意图解析、真实 POI 检索与确定性约束求解的智能出行规划平台。

用户可以直接描述复杂需求：

> 明天下午两点从学校出发，先取快递，再找一家评分高的蛋糕店，顺路买水果，五点前到医院，尽量少走路。

MapGo 不让大模型“凭感觉”安排路线，而是把职责拆开：

```text
自然语言
  → 结构化意图（LLM / 规则降级）
  → Pydantic 校验与歧义检测
  → 高德 POI 候选
  → 路线时间矩阵
  → 精确 TSP 或最近邻 + 2-opt
  → 截止时间与停留时间校验
  → 逐站 ETA、冲突原因与可解释结果
```

核心原则：**模型负责理解，地图服务提供事实，算法负责求解，后端验证硬约束。**

## 核心能力

- 自然语言提取起点、任务、交通方式、停留时间、截止时间和软偏好；
- `PlanningIntent` Pydantic Schema 校验模型结构化输出；
- 模糊地点返回 `need_clarification`，不让模型自行猜测；
- 6 个以内站点全排列求可行精确解，更多站点采用最近邻 + 2-opt；
- 硬截止时间不满足时返回 `infeasible` 和具体冲突；
- `Idempotency-Key` 防止重复点击造成重复模型调用；
- 模型与地图接口均可替换，Mock Provider 让 CI 不依赖真实 Key；
- FastAPI、SQLAlchemy 2.x 异步 ORM、PostgreSQL、Alembic；
- 原有 16 种地图模式、GPS、收藏、计划、分享和社交界面继续保留。

## 架构

```text
public/ 原生 JS + 高德 JS API
        │ HTTP / JSON
backend/app/main.py
        ├── api/          认证、个人数据、分享、管理、AI 规划
        ├── schemas/      Pydantic 输入/输出与意图模型
        ├── services/     意图解析、规划编排、路线优化
        ├── clients/      高德 / Mock 地图提供器
        ├── models.py     SQLAlchemy 数据模型
        └── db/           异步 Session 与 Alembic migrations
```

旧版 Node 服务仍保留在 `server.js`，用于迁移期回归对照；默认入口已经切换为 FastAPI。

## 快速开始

### 本地 SQLite

需要 Python 3.10+：

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r backend/requirements.txt
copy .env.example .env
python -m uvicorn backend.app.main:app --reload --port 3000
```

打开 <http://localhost:3000>。API 文档位于 <http://localhost:3000/docs>。

未设置 `AMAP_WEB_KEY` 时，AI 后端自动使用稳定的 Mock 地图数据，方便本地开发和测试；前端地图本身仍需要高德 Web JS API Key。

### Docker Compose + PostgreSQL

```bash
docker compose up --build
```

Compose 会启动 FastAPI 和 PostgreSQL 16，并将数据库持久化到命名卷。

## 配置

| 变量 | 用途 |
|---|---|
| `DATABASE_URL` | SQLAlchemy 异步数据库地址 |
| `AMAP_WEB_KEY` | 后端 POI / 距离 API 的 Web 服务 Key |
| `AMAP_KEY` | 前端高德 JS API Key |
| `AMAP_JSCODE` | 前端 JS API 安全密钥，只存服务端 |
| `LLM_API_KEY` | OpenAI 兼容模型服务 Key；为空时规则解析降级 |
| `LLM_BASE_URL` | OpenAI 兼容 `/v1` 地址 |
| `LLM_MODEL` | 意图解析模型名 |
| `ADMIN_INIT_TOKEN` | 第一个管理员账号初始化令牌 |
| `MOCK_MAP_PROVIDER` | 强制使用 Mock 地图提供器 |

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
  "default_service_duration_minutes": 15
}
```

结果状态：

- `success`：找到满足全部硬约束的路线；
- `need_clarification`：缺少起点、POI 不存在或候选地点有歧义；
- `infeasible`：当前时间与交通方式无法满足截止时间。

统一错误包含稳定错误码和 Request ID：

```json
{
  "ok": false,
  "code": "IDEMPOTENCY_KEY_REUSED",
  "msg": "同一个幂等键不能用于不同请求",
  "request_id": "req_...",
  "details": {}
}
```

## 测试

```bash
python -m pytest -c backend/pytest.ini
npm test
npm run test:e2e
```

Python 测试覆盖意图解析、中文相对日期继承、硬截止时间、精确求解、最近邻 + 2-opt、认证、AI 规划和幂等重放。CI 同时保留旧 Node API 回归测试与 Playwright E2E。

离线评测数据应记录真实指标，不在实现前填写虚构数字。推荐持续统计：

- 结构化输出合法率；
- 任务数、截止时间、交通方式识别准确率；
- 地点歧义发现率与约束满足率；
- 端到端成功率、P50/P95 延迟、Token 和费用。

运行当前 50 条基线集：

```bash
python -m backend.tests.evaluation.evaluate_intent
```

当前规则降级解析器实测：结构合法率 100%、任务数准确率 92%、交通方式准确率 100%、截止时间准确率 96%。这些数字只代表仓库内固定规则基线，不冒充真实大模型效果；更换模型后应重新运行并记录模型名、提示词版本与日期。

## 目录

```text
mapgo/
├── backend/
│   ├── app/
│   │   ├── api/
│   │   ├── clients/
│   │   ├── core/
│   │   ├── db/migrations/
│   │   ├── schemas/
│   │   └── services/
│   └── tests/
├── public/
├── src/                 # 迁移期 Node 旧后端
├── test/                # Node / Playwright 回归测试
├── Dockerfile
└── docker-compose.yml
```

## 安全与可靠性

- 密码使用带盐 scrypt，并兼容验证旧 Node 密码格式；
- 会话表只保存 Token 的 SHA-256 哈希，原始 Token 仅返回一次；
- Pydantic 校验、请求体大小限制和统一异常映射；
- 高德安全密钥与模型 Key 不下发浏览器；
- 外部地图请求设置超时，只对超时、429 和部分 5xx 有限重试；
- 创建 AI 计划支持请求指纹与幂等冲突检测。

## License

[MIT](LICENSE)
