# ADR-0002：三个隔离 Agent 与确定性编排器

- 状态：Accepted
- 日期：2026-08-14

## 背景

MapGo 已有 LLM 意图解析和 Companion Agent 工具循环。继续把地图查询、路线求解或权限判断包装成 Agent，会增加不可重复性、延迟和成本，却不会增加真实能力。同时，意图理解、方案体验审阅和行中风险响应具有不同上下文、权限与失败边界，使用同一个通用 Prompt 会扩大权限面并污染上下文。

## 决策

系统固定为三个 Agent：

1. `Intent Agent`：无工具权限，只输出类型化意图和澄清问题，不生成 POI；
2. `Critic Agent`：无工具权限，只基于已给方案和证据输出 `ReviewReport`，最多建议调整软目标权重；
3. `Companion Agent`：只在 Trip Session 中运行，只能选择 Companion 白名单工具，并继续经过状态、Consent、确认和预算 Policy。

三个 Agent 不互相直接调用。确定性 `PlanningAgentOrchestrator` 负责结构化 Artifact handoff、次数/成本预算、一次有界软重算和失败降级。地图 Provider、联合求解器、硬约束 Validator、Plan Version/Patch 和权限系统不是 Agent。

`off` 模式关闭 Critic；`shadow` 模式记录审阅但不改变规划结果；`enforce` 模式允许 Critic 阻止证据不完整的正式版本，或触发最多一次仅调整软权重的重新求解。无论哪种模式，Agent 都不能写入正式计划。

## 后果

- 每个角色拥有独立输入/输出 Schema、Prompt 版本、工具集合和预算；
- handoff 使用带来源、哈希、置信度和证据引用的版本化 Artifact；
- Agent Workflow、每角色 Run 和 Artifact 可审计并带同一 Trace ID；
- Critic 故障自动回退到规则审阅；Intent 保留原有规则解析回退；Companion 保留规则决策回退；
- 多一次 Critic 调用可能增加延迟与费用，因此默认使用 shadow、设置工作流总成本上限，并以离线评测和 Prometheus 指标决定是否切换 enforce。

