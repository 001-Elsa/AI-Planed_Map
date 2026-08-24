# MapGo v6 面试讲解手册

> 本文只描述当前 FastAPI / AI-Planned 实现。v1～v5 的 Node/SQLite 与经典前端 TSP 是历史能力，见 `CHANGELOG.md`；不要把历史架构当成当前正式后端。

## 一句话介绍（30 秒）

> MapGo 是一个 AI 行程规划与伴游系统。LLM 只把自然语言解析成严格结构化意图，真实 POI 必须来自地图 Provider，候选地点选择、访问顺序和时间窗由确定性求解器完成。行程中发生偏航、暴雨、延误或地点关闭时，Worker 会驱动受工具白名单、状态机、授权和预算限制的 Agent 生成待确认 Plan Patch；用户确认并通过当前 Patch 复验后，系统才创建新的正式计划版本。

当前技术栈：Python 3.12、FastAPI、Pydantic、SQLAlchemy 2.x、Alembic、PostgreSQL 16、Redis 7、OR-Tools、httpx、原生 ES Modules、高德地图、Playwright、Docker Compose、Prometheus/Grafana、GitHub Actions。

## 核心链路

### 首次规划

```text
POST /api/ai/conversations 或 /api/ai/plans
  → 配额 / 成本 / 幂等校验
  → LLM 或 RuleBased Intent Parser
  → 动态澄清
  → 并发召回真实 POI
  → 路线矩阵
  → Exact / OR-Tools / Beam 联合求解
  → 初次规划硬约束验证与启发式不确定区间
  → PlanningRun + PlanVersion V1
```

重点代码：

- `backend/app/services/planning_service.py`
- `backend/app/services/intent_parser.py`
- `backend/app/services/clarification.py`
- `backend/app/services/route_optimizer.py`
- `backend/app/api/ai_planner.py`

### 动态重规划

```text
TripEvent
  → Redis trip-events 队列
  → Worker 行程级锁
  → Agent Observation / Decision / Policy / Tool
  → propose_replan
  → pending PlanPatch
  → SSE 最新状态快照
  → 用户接受或拒绝
  → 路线重算与当前 Patch 约束复验
  → PlanVersion N+1 或保持 N
```

重点代码：

- `backend/app/services/trip_events.py`
- `backend/app/worker.py`
- `backend/app/services/agent_controller.py`
- `backend/app/services/agent_policy.py`
- `backend/app/services/replanning.py`
- `backend/app/api/companion.py`

## 技术难点与高频追问

### 1. 为什么不让 LLM 直接生成路线？

模型擅长理解“少走路、五点前到医院”等非确定语言，但不能证明 POI 真实存在，也不能稳定满足时间窗。MapGo 把 LLM 限制在意图和行动提案层：POI 来自 Provider，路线来自求解器，正式变更来自用户确认后的 PlanVersion。安全边界是 Schema、Provider、Policy、数据库权限和 Validator 的组合，不是 Prompt。

### 2. 求解的到底是什么问题？

不是只给固定地点做 TSP，而是同时为每项任务选择一个候选 POI，并决定任务访问顺序，可视为带候选选择、时间窗和业务约束的开放路径 TSP/VRPTW 扩展。

精确搜索空间约为：

```text
每项任务候选数的乘积 × 任务数阶乘
```

当前仅当任务数不超过 6 且总搜索空间不超过 60,000 时精确枚举；否则尝试 OR-Tools，求解失败才进入 Beam Search。不要把当前后端说成“超过 6 个点就最近邻 + 2-opt”。最近邻 + 2-opt 仍存在于前端“经典规划”备用流程 `public/js/services/algo.js`，不是 AI 后端主求解器。

### 3. 硬约束和软目标有什么区别？

硬约束决定方案是否可用，例如 deadline、最晚返回、步行上限、任务顺序、营业、无障碍、费用和区域；软目标只在候选方案间排序，例如更短时间、更少步行、更高评分、更低费用和更小改动。软分数不能抵消硬约束冲突。

### 4. 为什么既有 Redis 锁又有数据库版本？

Redis 锁用于减少同一行程同时运行多个 Agent/重规划任务；`base_version`、数据库事务和 `(planning_run_id, version)` 唯一约束保护最终正式数据。分布式锁只是协调手段，数据库约束才是最后正确性边界。

### 5. 如何防止模型调用危险工具？

模型输出先过严格 JSON Schema；工具名必须在白名单中；Controller 根据 Trip State、Consent 和确认要求再次判定；每个 run 还有步数、历史工具次数、Token 和费用限制；工具调用结果写入 AgentRun/AgentToolCall。模型没有直接写 PlanVersion 的工具。

### 6. 上游地图失败怎么办？

AMap Provider 有连接池、并发信号量、超时、针对 429/5xx 的指数退避和简单熔断器。路线矩阵先生成显式 haversine 估算矩阵，再用 Provider 返回的边覆盖；缺失或无 duration 的边保留 `quality=estimated`、较低 confidence 和 `fallback_used=true`。系统不会把估算包装成精确实时 ETA。

### 7. Session 为什么不使用 JWT？

服务端 Session 便于登出、设备撤销和删除用户后即时失效，适合涉及精确位置和正式计划的数据。原始 64 hex Token 只返回客户端，数据库保存 SHA-256；注册密码要求 8～64 个字符，使用 scrypt，并在工作线程计算以免阻塞事件循环。公开分享不是 Session：新分享使用独立的 128 bit（32 hex）capability token，旧 16 hex 链接仅为兼容继续读取。

### 7.1 请求入口如何防滥用和资源膨胀？

入口层同时做实际请求体字节上限、IP 总额度和身份粒度固定窗口限流。登录/注册不能使用可伪造的设备 Header 生成新预算，因此按来源 IP 计数；已登录 API 再按 Session Token 摘要计数。外部 Request/Trace ID 只接受有限长度的安全字符，未匹配路由统一聚合为 `__unmatched__`，避免 Prometheus 标签基数被随机 URL 撑大。内存 RuntimeStore 限制缓存条目、总字节数和计数器 key 数，计数器洪泛时失败关闭；生产多实例使用 Redis。

高德安全代理还使用精确路径白名单、独立 IP 限流、流式响应体上限和更小的缓存上限；只有合法、成功且未超过 `AMAP_PROXY_MAX_CACHE_BYTES` 的 JSON 响应进入缓存。API 默认返回 `Cache-Control: no-store`，并限制摄像头、麦克风和跨源定位能力。

### 8. 如何保护精确位置？

必须处于允许定位的 Trip State 并获得明确 Consent；坐标使用 Fernet 字段加密，默认短期 TTL，支持导出和清除，行程完成后停止跟踪。生产环境缺少位置加密密钥时应用拒绝启动。

### 9. 测试策略是什么？

- unit：解析器、约束、Agent Policy、路线算法、隐私；
- property：随机生成路线验证不变量；
- contract：Provider 部分数据、非法 POI、熔断；
- integration：真实 ASGI + SQLite，覆盖规划、版本、Worker、Agent、Patch；
- evaluation：RuleBased 意图解析质量门禁；
- chaos/load：锁竞争、重试、DLQ、429 时序与延迟分位数；
- Playwright：真实 uvicorn + 临时 SQLite 的浏览器冒烟。

测试数量和覆盖率以 `python -m pytest -c backend/pytest.ini --cov` 与 CI artifact 的当次输出为准，不背历史 Node 测试数字。

### 10. 当前最大的技术债是什么？

建议主动说出以下边界：

1. Redis List 没有 ACK/pending reclaim，`BRPOP` 后 Worker 硬崩溃可能丢失在途事件；
2. 分布式锁没有续租，任务超过 TTL 时可能并发执行；
3. SSE 只保存最新状态快照，不支持逐条、无缺口回放；
4. Patch 接受阶段只复验 deadline、最晚返回、步行和总费用，尚未复用首次规划全部约束；
5. OR-Tools/Beam 搜索使用固定近似代价，大规模问题不保证请求权重下的全局最优；
6. 同步求解仍运行在 API 进程内，高并发时应隔离到线程池、进程池或独立规划 Worker；
7. RAG 是本地 TF-IDF，不是向量数据库；真实 Push/邮件、完整 OpenTelemetry 和在线 ETA 校准尚未完成。

## 当前简历写法

**MapGo AI Planner —— AI 行程规划与动态伴游平台**

技术栈：FastAPI、Pydantic、SQLAlchemy、PostgreSQL、Redis、OR-Tools、Alembic、httpx、原生 JavaScript、高德地图、Docker、GitHub Actions

- 设计“LLM 意图理解 / Provider 事实 / 确定性求解 / Plan Version”边界，使用严格结构化输出、规则降级和动态澄清，避免模型虚构 POI 或直接修改正式行程；
- 实现候选 POI 与访问顺序联合优化：小搜索空间精确枚举，大规模使用 OR-Tools 时间窗路由并以 Beam Search 回退，统一验证时间、评分、步行、费用、无障碍和区域等约束；
- 实现 Trip Session 与受限 Agent 工具循环，按状态、Consent、步数、Token/费用预算执行；偏航、延误、暴雨和闭馆事件只生成待确认 Plan Patch，用户接受后才创建 Version N+1；
- 基于 Redis 实现配额计数、事件队列、ZSET 延迟重试、DLQ、行程级分布式锁和最新状态通知；基于 PostgreSQL/Alembic 保存幂等记录、不可变版本和决策审计；
- 建立 Ruff/Mypy/Bandit/pip-audit、pytest unit/property/contract/integration、AI eval、chaos/load、Playwright E2E 与 Docker build 的 CI 质量门禁。

## 3 分钟演示动线

1. 输入含截止时间和模糊偏好的自然语言；
2. 回答一次澄清，展示类型化 intent、真实候选、算法名、置信度和 V1；
3. 创建 Trip Session，注入延误或暴雨事件；
4. 展示 AgentRun/AgentToolCall 与 pending Patch；
5. 强调用户确认前 V1 未变化；
6. 接受 Patch，展示约束重算和 V2；
7. 用 pytest/CI 收尾，测试数字以现场输出为准。

## 面试禁止使用的旧口径

- “当前后端是纯 Node.js / node:sqlite / 零第三方依赖”；
- “后端超过 6 个点采用最近邻 + 2-opt”；
- “所有消息 Exactly Once”；
- “SSE 支持完整历史事件回放”；
- “Patch 已复验首次规划的全部硬约束”；
- “RAG 使用向量数据库”；
- “ETA 已完成在线历史校准”；
- “Web Push / 邮件已经真实投递”；
- “已接入完整 OpenTelemetry 全链路”。
