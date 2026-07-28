# MapGo AI-Planned 架构

## 决策边界

MapGo 采用模块化单体。当前规模不需要为了展示技术名词拆微服务；规划、身份和个人数据共享事务边界，耗时事件处理在 P2 才引入独立 Worker。

```text
Web Client
   │
FastAPI Modular Monolith
   ├─ Identity / Personal Data
   ├─ Intent Parser (LLM or deterministic fallback)
   ├─ Map Provider (AMap or Mock)
   ├─ Joint Planner
   ├─ Constraint Validator
   └─ Plan Version / Patch Policy
   │
PostgreSQL
```

## 一次规划

1. API 校验身份、请求体、配额边界与幂等指纹。
2. Parser 只提取用户表达，不生成 POI。
3. 缺少可验证硬约束时返回类型化澄清问题。
4. Map Provider 并发召回每个任务的候选 POI。
5. 全部候选合并成受 `MAX_ROUTE_MATRIX_POINTS` 限制的矩阵。
6. 求解器同时选择每个任务的一个候选并决定访问顺序。
7. Validator 检查时间窗、任务顺序、评分、步行和总时长。
8. 成功或不可行结果都保留事实来源、置信度和冲突。
9. 非澄清结果写入 `plan_versions` 的 Version 1。

## 联合求解

站点和候选数较小时，求解器枚举候选组合与访问排列，得到可用于回归测试的精确基准。搜索空间超过阈值后切换联合 Beam Search，避免请求耗时不可控。

目标函数包含：

```text
travel_time
+ walking_time
+ distance
+ low_rating_penalty
+ uncertainty_penalty
```

权重位于请求的 `preferences.weights`，硬约束不通过时不会用软目标“抵消”。

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

## 数据可信度

每条路线边的 `source/quality/traffic_timestamp/confidence/fallback_used` 是正式 API 合同。任何回退都降低置信度并在前端显示“估算”，不能以精确 ETA 的口吻呈现。

## 下一阶段边界

伴游 Agent 将复用 Planner 与 Patch Policy，而不是绕过它们。事件 Controller 只产生 Action Proposal；涉及删除站点、费用、位置分享或长期偏好的动作必须经过 Policy 和用户确认。
