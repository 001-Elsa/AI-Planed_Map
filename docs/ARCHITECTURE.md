# MapGo AI-Planned 架构

## 决策边界

MapGo 采用模块化单体。规划、身份和个人数据共享事务边界；耗时事件由独立 Worker 消费 Redis 队列，但与 API 共享同一代码库与数据模型。

```text
Web Client
   │
FastAPI Modular Monolith
   ├─ Identity / Personal Data
   ├─ Intent Parser (LLM + RuleBased runtime fallback)
   ├─ Map / Weather / Knowledge Providers
   ├─ Joint Planner (Exact / OR-Tools / Beam fallback)
   ├─ Initial-plan Validator / Patch Recalculator
   ├─ Plan Version / Patch Policy
   ├─ Companion Trip Session + Agent Controller
   └─ Runtime Store (Redis or in-memory)
   │
Worker  ←── Redis queues / locks / pubsub
   │
PostgreSQL (Compose / CI) or SQLite (local / E2E)
```

```mermaid
sequenceDiagram
    participant E as "高风险事件"
    participant W as "Worker + 分布式锁"
    participant A as "Agent Controller"
    participant P as "Policy + Tools"
    participant R as "Replanner"
    participant U as "用户"
    E->>W: 偏航 / 延误 / 暴雨 / POI 关闭
    W->>A: Trip State + Observation
    loop 受限工具循环
        A->>P: LLM 选择白名单工具
        P-->>A: 位置 / 天气 / 行程事实
    end
    A->>R: propose_replan（只可创建 pending）
    R-->>W: Patch + 影响对比
    W-->>U: SSE 推送待确认方案
    U->>R: 确认
    R-->>U: 约束复验通过后 Version N+1
```

## 一次规划

1. API 校验身份、请求体、配额边界与幂等指纹。
2. Parser 只提取用户表达，不生成 POI；LLM 故障时降级到规则解析器。
3. 缺少可验证硬约束时返回动态澄清问题。
4. Map Provider 并发召回每个任务的候选 POI。
5. 全部候选合并成受 `MAX_ROUTE_MATRIX_POINTS` 限制的矩阵。
6. 求解器同时选择每个任务的一个候选并决定访问顺序。
7. 初次规划 Validator 检查时间窗、任务顺序、评分、营业状态、无障碍、步行、总时长、费用、区域与不确定缓冲。
8. 成功或不可行结果都保留事实来源、置信度和冲突。
9. 非澄清结果写入 `plan_versions` 的 Version 1。

## 联合求解

站点和候选数较小时，求解器枚举候选组合与访问排列，得到可用于回归测试的精确基准。搜索空间超过阈值后优先尝试 OR-Tools 时间窗路由，再回退联合 Beam Search。

目标函数包含：

```text
travel_time
+ walking_time
+ distance
+ low_rating_penalty
+ uncertainty_penalty
+ monetary_cost
+ change_penalty
```

权重位于请求的 `preferences.weights`，硬约束不通过时不会用软目标“抵消”。精确枚举会用完整权重比较全部候选；当前 OR-Tools/Beam 的搜索阶段使用固定的近似代价，随后再用完整评价器验证其结果，因此大规模搜索尚不能保证找到请求权重下的全局最优可行方案。

## Plan Patch

计划调整遵循 optimistic concurrency：

```text
Version N
  → Patch(base_version=N, pending)
  → 用户确认
  → 路线矩阵重算
  → 硬约束复验
  ├─ blocked: Version N 保持不变
  └─ allowed: 生成 Version N+1
```

所有路径写入 `decision_audit_logs`，包括阻止执行的规则。

Patch 操作包括 `remove_stop`、`move_stop`、`replace_stop`、`change_transport_mode`。接受时会在新地点与新交通方式下重新计算路线，并复验站点 deadline、最晚返回、步行上限和总费用；这些检查产生冲突时会阻止新 Version 写入。评分、营业、无障碍、区域、任务顺序和最大总时长等首次规划约束尚未统一复用到 Patch 接受路径，这是当前明确的技术债。

## 伴游与 Worker

- Trip Session 管理状态机、Consent 与定位 TTL；
- 位置更新可自动做路线走廊偏航检测并生成 `UserOffRoute`；
- Agent Controller 按 Observation → LLM Decision → Policy → Tool → Observation 循环编排。模型只能输出白名单工具或结束，不能写入 PlanVersion；无 LLM 或 LLM 调用失败时降级为同一边界内的规则决策器；
- Worker 通过 Redis List 消费 `mapgo:trip-events` / `mapgo:notifications`，失败时进入 ZSET 延迟重试，耗尽后进入死信队列；
- 高影响事件可自动产生待确认 Patch。Worker 锁和事件状态避免重复消费生成多个 Patch；Patch 应用仍需要用户确认。

当前 Worker 的可靠性边界：`BRPOP` 会先移除消息，再执行数据库处理；捕获到的异常会重新入队，但进程在处理期间硬崩溃时可能丢失在途消息。行程锁使用 `SET NX EX` 与 token 校验释放，但没有租约续期。生产化应考虑 Redis Streams Consumer Group、消息代理或数据库 outbox/inbox 与锁续租/fencing token。

## SSE 与事件可见性

API 和 Worker 会把最新行程事件写入 `trip-stream:{trip_id}`，SSE 端点轮询这一快照并发送给已认证客户端。`Last-Event-ID` 只避免重连后重复发送同一快照；如果多个事件在两次轮询之间到达，中间事件可能被最新快照覆盖，因此当前 SSE 不是持久化事件日志。

## 运行时并发边界

地图和数据库调用使用异步 I/O；联合求解器本身是同步 CPU 工作。虽然搜索空间和 OR-Tools 时间限制受到配置约束，它仍运行在 API 事件循环所在进程中。更高并发场景应将求解隔离到 `asyncio.to_thread`、进程池或独立规划 Worker。

## 数据可信度

每条路线边的 `source/quality/traffic_timestamp/confidence/fallback_used` 是正式 API 合同。任何回退都降低置信度并在前端显示“估算”，不能以精确概率 ETA 的口吻呈现。

当前规划时使用的是 Provider 置信度与安全缓冲构成的启发式区间。`calibrate_from_history()` 仍是离线辅助函数，尚未获得按交通方式、时段聚合的真实 ETA 样本，所以不作为在线“历史残差校准”能力宣称。
