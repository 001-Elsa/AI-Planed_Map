# Agent 系统改造前差距分析

> 审计日期：2026-08-27
> 审计对象：当前工作树（包含审计开始前已有的未提交改动）
> 审计阶段结论：本阶段只建立事实基线，不新增 Agent，不替换已有正确模块。

## 1. 审计目标与原则

本次审计围绕 Supervisor、Agent Orchestrator、Agent Message Protocol、Shared State、Runtime Store/CAS、Context/Memory、Tool Registry、Model Router、Critic、HITL、PlanPatch/PlanVersion、确定性 Planner/Solver/Validator、Worker Queue/Retry/DLQ、Evaluation、Prometheus 与 Trace 展开。

后续改造遵守以下边界：

1. 不以 Agent 数量作为复杂度或成熟度指标；
2. Search、Safety、Planner、Replanner 中的确定性逻辑继续保持确定性，不改成自由 LLM 调用；
3. 不删除后重新实现已经正确存在的 Registry、CAS、Solver、Validator、Plan Version 等边界；
4. 优先补齐真实执行、可恢复性、评测可信度和运行证据，而不是扩展角色名词；
5. 文档声明必须能追溯到代码、测试或可复现实验；没有真实 LLM/真实基础设施证据时明确标记为缺失。

## 2. 仓库状态与审计范围

审计开始时工作树不是 clean 状态。已有改动涉及 `README.md`、`docs/ARCHITECTURE.md`、MCP、长期 Memory、Search/Planner Tool Adapter、API 和测试等。本报告将这些内容视为当前实现的一部分，没有回滚、覆盖或重新创建同类模块。

已阅读和核对：

- `README.md`、`docs/ARCHITECTURE.md`、`docs/EVIDENCE.md`、`docs/INTERVIEW.md`；
- `docs/adr/0001` 至 `0019`；
- `backend/app/services/`、`backend/app/services/agents/`、`backend/app/infrastructure/runtime_store.py`、`backend/app/worker.py`；
- Agent/Plan 相关 Schema、ORM 模型和 Alembic `0010` 至 `0013`；
- `backend/tests/` 中 unit、integration、property、contract、evaluation、chaos、load 入口；
- `.github/workflows/ci.yml`、`pyproject.toml`、`backend/pytest.ini`、`Makefile`、`package.json`。

## 3. 总体判断

当前仓库已经超过“只有 README 的 Multi-Agent Demo”：权限隔离、确定性求解、Plan Version/CAS、Shared State、受限工具循环和审计表都有真实代码。主要问题不是缺少更多 Agent，而是部分抽象尚未成为真实生产执行路径，现有 Benchmark 对优势的证明力度不足，真实 LLM、真实 Redis 故障和跨进程 Trace 证据缺失。

当前最准确的定位是：

> 已具备较完整生产边界的模块化 Agent 系统原型；确定性规划和安全控制较扎实，但动态任务图、分布式 Agent Transport、真实 LLM Evaluation、基础设施级故障注入和端到端观测仍未闭环。

## 4. 当前已经真实实现的能力

| 能力 | 实现证据 | 测试/运行证据 | 审计结论 |
| --- | --- | --- | --- |
| Supervisor 与规划编排 | `agent_orchestrator.py` 执行 Supervisor → Intent → Search → 可选 Safety → Planner → Critic → Final；支持一次有界软重算和 Search recovery | `test_multi_agent_isolation.py`、`test_executable_planning_agents.py` | 已实现；实际执行仍是进程内编排 |
| 结构化消息协议 | `agent_protocol.py` 校验 route allowlist、payload schema、content hash、因果 ID、幂等键、过期时间并生成最小化审计 | 协议篡改、越权、硬约束夹带、伪造 Shared State 引用测试 | 已实现且失败关闭 |
| Shared State 与 CAS | `agent_shared_state.py` 提供角色读写矩阵、revision、state hash、TTL、状态迁移和主动删除；`runtime_store.py` 提供内存锁/Redis Lua CAS | 生命周期、并发冲突、篡改、超限、版本单向刷新测试 | 已实现；是当前较强的边界之一 |
| Tool Registry | `agent_tool_registry.py` 区分 `agent_callable`、`internal_stage`、`workflow_only`，要求角色、调用模式和数据域完全匹配 | Registry、跨角色提权、scope 多报/少报、内部能力不进入 LLM schema 测试 | 已实现且失败关闭 |
| Typed Tool Contract | `agent_tool_contracts.py` 用 Pydantic 固定参数并返回稳定 `ToolResultEnvelope`；错误不透传上游异常文本 | Runtime、Planning Tool 和 Adapter 测试 | 已实现 |
| Context Engineering | `agent_context.py` 构建 Planning/Critic/Companion 角色切片，校验 state revision/hash 和 artifact hash；Critic 模型上下文有注入文本清洗与 16k 上限 | stale/tamper、最小上下文、Prompt injection 测试 | 已实现核心边界 |
| 短期/长期 Memory | 短期使用 Shared State 并在规划 `finally` 主动删除；长期偏好经 `UserPreferenceMemory`、固定 schema、显式确认、覆盖优先级、禁用/撤销/清除 | Memory unit 与 API integration 测试 | 已实现；长期 Memory 不是 Agent 自主画像 |
| Model Router | `model_router.py` 对 Intent/Critic/Companion 选择 Rule/Small/Strong，对 Supervisor/Search/Safety/Planner/Replanner 锁定 deterministic；记录价格、风险和回退 | 12-case 路由门禁与 Runtime wiring 测试 | 已实现静态策略 |
| Critic | Rule Critic 与 Strong LLM Critic 共用确定性路线评估；硬失败覆盖模型结论；支持 off/shadow/enforce 和一次软权重重算 | Critic、route evaluation、soft retry、readiness 测试 | 已实现主要控制流 |
| HITL | 初次规划对高步行/高费用生成 confirmation；拒绝转换为结构化约束后重跑；动态高风险 Patch 默认需确认 | `test_human_in_loop.py` 与 replanning integration | 已实现初始风险门槛 |
| 确定性规划 | `route_optimizer.py` 实现候选选择与顺序联合求解：Exact、OR-Tools、Beam fallback；Planner 通过内部 capability 调用矩阵/求解/公交边复核 | optimizer、property、provider contract、planning integration | 已实现，不应 Agent 化 |
| Patch/Version/CAS | `plan_patch_validator.py` 结构应用后复用 `evaluate_joint_order`；`plan_versioning.py` 锁 Patch/最新 Version、校验 base version、写 N+1、依赖唯一约束防双写 | 单写者、闭馆替换、deadline 复验、接受前版本不变测试 | 已实现核心一致性边界 |
| Worker 主队列恢复 | Runtime Store 支持 reserve/processing/ack、启动 recover、延迟 retry、DLQ、token lock renew；Worker 对事件幂等并在异常后入 retry | Runtime Store、Agent replanning integration、chaos smoke | 已实现基础机制；仍有生产级缺口，见后文 |
| 可恢复 Agent Message Transport | `agent_transport.py` 实现内存 transport 和 Redis Streams consumer group、ACK、PEL reclaim、retry、DLQ、发布幂等 | 内存实测与 Fake Redis 单测 | Transport 本身已实现 |
| 动态重规划 | `dynamic_replanning.py` 固定执行 Companion → Supervisor → Replanner → Planner → Critic，生成 Patch；Replanner 仅选策略，求解仍为确定性函数 | unit/integration 覆盖闭馆、天气、工具越权和 CAS | 已实现主要业务闭环 |
| Evaluation | 意图、Agent 隔离、路线、静态 multi-agent graph、Model Router、100-case replay 均有可执行脚本并进入 CI | 本次全部离线门禁通过 | 已实现离线回归框架 |
| Prometheus 与审计 | Agent route/cost/latency/recovery/capability/shared-state/worker 指标；AgentWorkflow/Run/Task/Handoff/Artifact/Message/ToolCall 和 DecisionAudit 表 | API/DB integration 与覆盖率结果 | 已实现基础指标和持久化审计 |
| 可选 MCP Adapter | 当前工作树含 Local/HTTP/MCP adapter、本地 schema pin、Registry 前置授权和默认关闭的只读 `/mcp` | MCP adapter 与 API integration 测试 | 已实现可选传输层；不改变权限源 |

## 5. 当前只有部分实现的能力

### 5.1 Supervisor 的“动态任务图”尚未成为通用图执行器

`SupervisorAgent` 会生成带依赖关系的 `AgentExecutionPlan`，但 `PlanningAgentOrchestrator.execute_planning_stages()` 仍按固定 Python 顺序调用 Search、可选 Safety 和 Planner。任务节点不是由通用 DAG scheduler 按依赖、状态、attempt 和 budget 驱动。

更具体地说，Supervisor 能为含天气不确定性的请求生成 `weather` 节点，但 Orchestrator 只检查并执行 `search`、`safety_check`、`planner`，没有 weather 节点执行方法、协议 route、Shared State 字段或对应测试。现有测试只验证“图中出现 weather”，没有验证真实执行。因此该节点属于声明已生成、执行未落地。

### 5.2 持久化 Task Graph 与 Supervisor 原图不完全一致

`persist_agent_workflow()` 根据实际 trace 顺序重新生成 `01_role`、`02_role` 等 task key，并把每一步依赖简单设置为前一步；它没有持久化 Supervisor 原始 `depends_on`，也没有真实记录 pending/running/retry/blocked 的状态演进。重复角色（例如 Search/Planner/Critic 的软重算）只通过序号区分，无法直接还原 Supervisor 计划节点的版本和重试关系。

### 5.3 动态重规划的角色 Trace 部分是事后包装，不是角色真实执行

`DynamicReplanningOrchestrator` 中只有 `ReplannerAgent.run()` 是实际角色调用。Companion、Supervisor、Planner、Critic 的多段 trace 由 `_execution(spec, payload, ...)` 直接合成；实际求解调用的是 `create_pending_replan()`，实际审阅调用的是确定性 `review_dynamic_patch()`，并没有执行 `PlannerAgent.run()` 或 `CriticAgent.run()`。

确定性函数继续保持确定性是正确的，但把这些函数执行结果包装成多个“Agent 执行”会让审计表误以为对应角色在运行时真正取得并执行了任务。这与本次“不把普通函数强行包装成 Agent”的原则直接相关。后续应把它们记录为 deterministic stage/span，或者让既有角色接口成为真实、单一的执行 owner；不能继续用合成 AgentRun 增加 Agent 数量。

### 5.4 可恢复 Agent Message Bus 尚未接管生产规划链路

应用启动时创建 `agent_message_bus`，但同步规划仍使用 `AgentMessageRouter.deliver()` 加进程内 `deque`。仓库没有启动 `AgentTaskWorker` 来消费各角色 Redis Stream 的生产 Worker。ADR-0015 也明确同步 endpoint 不会把同一工作流镜像到 bus。

因此当前结论应是“具备可恢复 transport 组件”，不能说“规划 Agent 已通过 Redis Streams 分布式执行和恢复”。

### 5.5 Worker Recovery 已加强，但还不是完整生产语义

主 Worker 已有 processing/ack/启动恢复和锁续租，这使 `docs/INTERVIEW.md` 中“Redis List 没有 ACK、锁没有续租”的说法过时。但仍有以下缺口：

- processing list 只在 Worker 启动时整体恢复，没有 per-message claim time、visibility timeout 或其他存活 Worker 的主动 reclaim；单个 Worker 崩溃而集群其余实例持续运行时，在途消息可能一直滞留；
- lock heartbeat 续租失败只记录日志并退出 heartbeat，业务处理不会立即中止，也没有 fencing token 阻止失去租约的执行者继续提交；
- Redis retry promotion 使用 `ZREM` 后 `LPUSH` 两步，进程在两步之间崩溃存在丢失窗口；
- 真实 Redis restart、网络分区、慢 DB commit、成功提交后 ack 前崩溃尚无集成故障注入。

### 5.6 Model Router 是可测试的静态策略，还没有效果闭环

Router 能正确锁定确定性角色并记录模型档位与价格，但当前门禁主要验证预设规则是否返回预设 tier。没有用同一真实 LLM 数据集比较 Rule/Small/Strong 的准确率、延迟、Token、成本和 fallback，也没有证明阈值优于固定模型策略。

`requires_hitl` 是路由决策字段；初次规划 HITL 实际由步行/费用规则触发，动态 Patch 则由 Patch risk 触发。尚无统一证据证明 Router 风险标记一定被下游审批消费。

### 5.7 Critic 已有确定性硬门禁，但线上质量准入偏粗

Readiness 使用 shadow 样本数、fallback rate、blocking rate、budget exceeded rate 和 p95 latency。它没有带人工标签的 precision/recall、误拦截率、漏拦截率、按场景分层结果和模型/Prompt 版本对比。当前把 blocking rate 高直接视为不健康，也无法区分“正确拦截了更多坏计划”和“误报增加”。

### 5.8 Context Engineering 有边界测试，缺少质量与 Token 评测

现有测试证明 stale/tampered context 会失败、Critic 注入文本会清洗，但没有衡量不同 Context 策略对任务成功率、关键信息保留率、Token 消耗和长对话污染的影响。Companion 的截断基于 `len(str(context))`，不是实际模型 tokenizer；也没有多轮长上下文 benchmark。

### 5.9 Evaluation 覆盖了回归，但覆盖面仍小

- Intent 集 54 条，其中 `avoid_hiking` 和 `travel_style` 各只有 4 条专项样本；
- Route 集只有 6 条；
- Agent Critic 离线集只有 4 条；
- 静态 multi-agent 60-case 门禁主要验证提交 JSON 中声明的 expected 字段，不执行生产工作流；
- 无真实 Provider 地域差异、用户满意度、中文复杂约束组合、模型漂移或 adversarial tool-result 大规模样本。

### 5.10 Trace/Observability 有记录，但没有完整分布式 Trace

当前有 Request/Trace ID、Prometheus 和数据库审计，但没有 OpenTelemetry span、跨 API/Worker/Redis/DB 的 parent-child 关系、trace sampling/export 和一条可视化端到端链路。进程内 metrics 也不能替代多实例聚合后的 SLO。

### 5.11 Runtime 预算是“调用后校验”为主

Agent Runtime 在模型返回后累加 Token/成本并判断超限；Critic 也是执行后计算 projected workflow cost。它能阻止继续执行，但不能防止本次模型调用本身超过剩余预算。缺少基于最大输出、输入估算和剩余预算的调用前 admission control。

## 6. 当前只有文档描述或实现证据不足的能力

| 声明 | 实际情况 | 结论 |
| --- | --- | --- |
| “真实 LLM 实验/Prompt 回归” | `.env` 中无 LLM endpoint/model/key 配置；本次所有评测的 LLM call、Token、cost 都为 0 | 未实现证据，不能宣称完成 |
| “动态 weather task 已执行” | Supervisor 只生成节点；Orchestrator 没有执行路径 | 文档/计划层存在，运行不足 |
| “Agent 规划链路已由可恢复消息队列驱动” | Redis Stream transport 未接入同步规划，未运行角色 Worker | 组件存在，系统级能力不足 |
| “动态重规划中的 Companion/Supervisor/Planner/Critic 都是可执行角色” | 多个 AgentRun/step 由 `_execution()` 合成，实际由确定性服务函数完成 | Trace 命名强于真实执行，应去除伪 Agent 化 |
| “Benchmark 证明真实动态重规划优势” | replay 的 weather/closed/off-route 通过再次运行 `PlanningService` 模拟；duplicate event 用局部 `set`；worker crash 用内存 transport，Single baseline 结果直接构造成失败 | Benchmark 可作离线回归，不能作生产因果证据 |
| “完整 Agent Evaluation” | 缺真实 LLM、真实 Provider、人工标注 judge calibration、线上反馈和长期漂移检测 | 未形成完整闭环 |
| “完整跨服务 Trace” | 只有 Trace ID、指标和审计记录 | 未实现 |

## 7. 文档与实现漂移

1. `docs/INTERVIEW.md` 仍声称 Redis List 没有 ACK/pending reclaim、锁没有续租、Patch 只复验部分约束；当前代码已经加入 processing/ack/启动恢复、`renew_lock` heartbeat 和共享联合评价器。该文档需要在后续改造中更新。
2. `README.md` 的 Benchmark 数字来自已提交快照；本次语义指标一致，但本机延迟已经变化。延迟应始终标注运行环境和时间，不能复制为固定性能结论。
3. `docs/EVIDENCE.md` 的 chaos 描述仍强调“没有 ACK/在途恢复语义”，与当前 Runtime Store 代码不一致；但它指出现有 chaos 脚本只模拟内存队列，这一限制仍然成立。
4. ADR-0016 对 Benchmark 的描述强于脚本实际执行。特别是“真实动态事件回放”和“Worker crash recovery”目前含人工模拟，应调整实现后再保留强结论。
5. `average_agent_count` 把 Supervisor、Search、Safety、Planner 等确定性角色/阶段都计作 Agent 数量。该指标与本次“不以 Agent 数量体现复杂度”的原则冲突，后续应改名为 active roles/stages，并把 autonomous LLM calls 单独统计。

## 8. 本次准备修改的模块

以下是后续改造范围，按优先级排序；本审计阶段尚未修改这些模块。

### P0：让证据可信

1. `backend/tests/evaluation/`：新增真实 LLM profile、Prompt/模型版本锁定、Token/成本/延迟记录、失败样本和可重复运行元数据；无凭证时明确 `skipped`，不得以 0 cost 冒充真实 LLM 结果。
2. `replay_agent_benchmark.py`：移除脚本内硬编码胜负与局部集合模拟，改为执行真实 DynamicReplanningOrchestrator、真实幂等存储和真实 Worker/Transport 路径；进行配对多次运行并输出置信区间或至少分场景样本数。
3. `backend/tests/chaos/` 与 CI：增加真实 Redis/PostgreSQL 服务上的 crash-before-ack、commit-before-ack、PEL reclaim、锁丢失、retry promotion 中断、重复投递和 CAS 竞争测试。
4. `docs/EVIDENCE.md`：建立机器生成结果索引、命令、commit/worktree 状态、Python/依赖/OS、数据集 hash 和时间戳，避免手工复制过期数字。

### P0：让动态编排与执行一致

5. `agent_orchestrator.py`、`supervisor_agent.py`、Agent task schema/persistence：以现有 `AgentExecutionPlan` 驱动通用的确定性 DAG 执行，补齐 weather 节点，持久化原始 dependencies、attempt、状态迁移和重试关系。
6. `dynamic_replanning.py`：去除 `_execution()` 合成 AgentRun 的做法。确定性求解/审阅记录为 stage；只有真实调用既有角色 runtime 时才记录 AgentRun。
7. `agent_transport.py`、应用/Worker 启动逻辑：明确两种互斥模式——同步进程内 owner 或分布式 message-bus owner；为分布式模式提供真实 role worker，避免双执行。

### P1：加深已有能力

8. `worker.py`、`runtime_store.py`：增加 processing reclaim 元数据/机制、锁丢失中止与 fencing、原子 retry promotion 或 outbox/inbox 方案及故障测试。
9. `model_router.py`、Intent/Critic/Companion 调用链：增加调用前预算 admission、离线/真实模型 counterfactual 评测和路由收益报告。
10. `agent_readiness.py`、`agent_evaluation.py`：用标注集计算 Critic precision/recall/false-block/false-pass，按 Prompt、模型、场景和风险分层；扩大 intent/route/adversarial cases。
11. `agent_context.py`：增加 tokenizer-aware budget、上下文消融、长会话污染、恶意 Provider 文本和关键信息保留评测。
12. Observability：在不改变业务边界的前提下加入跨 API/Worker/DB/Redis 的 OpenTelemetry spans，并建立 Agent SLI/SLO dashboard。

## 9. 明确不准备重复实现的模块

后续改造不会另起一套同名架构，也不会把以下确定性能力改成 LLM Agent：

- `route_optimizer.py` 的 Exact / OR-Tools / Beam 与 `evaluate_joint_order`；
- `plan_patch_validator.py` 和 `plan_versioning.py` 的验证、PlanVersion、CAS/唯一约束；
- `agent_tool_registry.py` 的角色/mode/scope 权限源；
- `agent_protocol.py` 的消息 envelope、route allowlist、hash/causality/idempotency 校验；
- `agent_shared_state.py` 与 Runtime Store CAS；
- `agent_memory.py` 的显式长期偏好边界；
- `human_in_loop.py` 和动态 Patch confirmation 的基本审批模型；
- `agent_runtime.py` 已有的 bounded tool loop；
- Provider Search、路线矩阵、Solver、Validator、Safety rule 和 Replanner strategy 等确定性模块。

若需要加强这些能力，采用扩展测试、接通真实执行路径、补充状态/指标或局部重构，不创建平行实现。

## 10. 当前技术债清单

| 优先级 | 技术债 | 风险 |
| --- | --- | --- |
| P0 | weather DAG 节点只规划不执行 | 任务图与真实行为不一致，审计可能误导 |
| P0 | Replay Benchmark 含硬编码失败与局部模拟 | 无法证明架构对真实故障的因果收益 |
| P0 | 无真实 LLM Evaluation | 无法证明 Model Router、Prompt、Critic/Intent 在真实模型上的质量和成本 |
| P0 | 动态重规划用 `_execution()` 合成多个 Agent step | 把确定性函数包装成 Agent，Trace 不能证明真实角色执行 |
| P0 | Agent Stream Transport 未接生产规划 owner | 可恢复 transport 不能代表系统已可恢复分布式执行 |
| P0 | Worker 在锁续租失败后仍可能提交 | 可能产生租约失效后的并发写；数据库 CAS 只保护部分写路径 |
| P1 | processing 仅启动恢复、retry promotion 非原子 | 长时间运行集群可能滞留或丢失消息 |
| P1 | 持久化 task dependency 被线性化 | 无法准确重放 Supervisor 原任务图 |
| P1 | Critic readiness 没有 precision/recall | blocking rate 无法区分正确拦截与误报 |
| P1 | Context budget 非 tokenizer-aware、无消融评测 | Token 成本与信息损失不可量化 |
| P1 | Model budget 主要调用后判定 | 单次调用仍可能突破剩余预算 |
| P1 | Agent Evaluation 样本规模小且场景分布单一 | 易对手写样本过拟合 |
| P1 | Trace 未跨 API/Worker/DB/Redis 串联 | 故障定位与延迟归因不完整 |
| P2 | OR-Tools/Beam 使用近似搜索代价 | 大规模问题不保证请求权重下全局最优，当前文档已诚实披露 |
| P2 | CPU 求解仍位于 API 进程线程 | 高并发下缺进程级资源隔离 |
| P2 | 文档互相漂移 | 面试/README/ADR 可能给出相互矛盾的能力口径 |

## 11. 改造前测试基线

### 11.1 环境

- OS：Windows（PowerShell）；
- Python：3.10.11；仓库/CI 目标为 Python 3.12；
- 工作树：非 clean，包含审计前已有改动；
- LLM：本地 `.env` 未配置 `LLM_API_KEY`、模型或 endpoint；
- 本次没有真实 Redis/PostgreSQL 外部故障实验，也没有真实 LLM 调用。

### 11.2 pytest

文档和 CI 中的原始命令：

```powershell
python -m pytest -c backend/pytest.ini --cov --cov-report=term-missing
```

在本机直接运行结果：收集失败，`ModuleNotFoundError: No module named 'backend'`，0% coverage。当前 Python 安装的固定 `sys.path` 指向另一个工程，且忽略 `PYTHONPATH`。这说明本地复现入口对当前环境不稳健，但不是测试断言失败。

在不修改仓库的前提下，用 bootstrap 显式插入仓库根目录后运行：

```powershell
python -c "import sys; sys.path.insert(0, r'D:\a_projects\mapgo'); import pytest; raise SystemExit(pytest.main(['-c','backend/pytest.ini','--cov','--cov-report=term-missing']))"
```

结果：

- `160 passed`；
- `3 warnings`，均为 OR-Tools/SWIG 类型的 DeprecationWarning；
- 总覆盖率 `74.52%`，高于 `fail_under=55`；
- wall time `90.58s`；
- Agent 相关较低覆盖模块包括 Worker `58%`、Dynamic Replanning `63%`、Agent Memory `64%`、Agent Planning Tools `66%`、Agent Decider `66%`；真实 Redis 路径大多没有被覆盖。

### 11.3 静态质量

- `ruff check backend`：通过；
- `ruff format --check backend`：通过，143 files already formatted；
- `mypy backend/app`：通过，95 source files 无问题。

### 11.4 离线 Evaluation

全部命令退出码为 0：

| 评测 | 基线结果 |
| --- | --- |
| Intent | 54 cases；schema 100%；task count 88.89%；transport 100%；deadline 96.30%；avoid hiking 100%；travel style 100% |
| Agent/Critic isolation | 4/4 verdict 正确；Registry、Tool、Shared State、Capability、Memory isolation 全部通过 |
| Route | 6 cases；expectation accuracy 100%；hard failure detection 100%；good route min score 100 |
| Static multi-agent | 60 cases；tag 完整；graph/handoff/terminal 声明均 100%；平均声明 subtasks 4.03 |
| Model Router | 12 cases；route accuracy 100%；确定性角色均为 deterministic；高风险 HITL 标记 100%；无凭证回退 rule |

注意：这些是离线规则与声明门禁，不是 live LLM 质量证明。

### 11.5 Chaos smoke

`python backend/tests/chaos/run_chaos.py` 退出码 0，4/4 通过：内存重启丢队列（按预期不可恢复）、内存锁竞争、内存 retry → DLQ、模拟 429 backoff。该脚本没有连接真实 Redis，不验证 Redis processing recovery、Streams PEL、网络分区或多 Worker crash。

## 12. 改造前 Benchmark 基线

执行命令：

```powershell
python backend/tests/evaluation/replay_agent_benchmark.py
```

环境与数据：

- profile：`offline_deterministic`；
- case count：100；
- 13 个 scenario template；
- dataset hash：`b6ac33236f0ace74fecedb244014cbd74dfeb79e614d99e658556c1adbad6d56`；
- 真实 LLM calls/tokens/cost：两组均为 0；
- 外部 Provider：未调用；
- 结果：进程退出码 0。

| 指标 | Single Controller | Supervised Multi-role |
| --- | ---: | ---: |
| Task Completion | 85.00% | 100.00% |
| Hard Constraint Satisfaction | 100.00% | 100.00% |
| Executable Plan Constraint Satisfaction | 100.00% | 100.00% |
| Tool Selection Accuracy | 92.00% | 100.00% |
| Illegal Tool Execution | 0.00% | 0.00% |
| Handoff Success | 100.00% | 100.00% |
| Recovery Success | 84.44% | 100.00% |
| Replanning Success | 100.00% | 100.00% |
| Critic Bad-plan Recall | 0.00% | 100.00% |
| Average Active Roles/Stages | 1.00 | 4.55 |
| P50 Latency | 23.32 ms | 66.19 ms |
| P95 Latency | 341.47 ms | 392.67 ms |
| Average LLM Calls / Token Cost | 0 / $0 | 0 / $0 |

与已提交 `replay_agent_benchmark_result.json` 相比，质量指标相同，本机延迟略有变化（已提交快照为 20.90/336.26 ms 与 64.47/390.69 ms）。这属于本机离线时延波动。

### Benchmark 证据边界

当前结果可以证明：在这 100 个确定性脚本案例和既定评分方法下，带 Supervisor/Safety/Critic/Transport 组件的 runner 通过了现有回归门禁。

当前结果不能证明：

- 真实 LLM 的准确率、稳定性、Token 成本或 Prompt 改造收益；
- 真实 Redis/PostgreSQL、跨进程 Worker、网络分区和 crash recovery；
- 动态重规划生产链路优于单 Controller，因为多个动态场景只是重新运行首次规划；
- 100% 指标可外推到真实用户、真实地图 Provider 或生产延迟；
- 更多 Agent/角色本身带来收益。当前 `average_agent_count` 实际统计的是活跃角色/阶段，应停止作为架构复杂度卖点。

## 13. 审计后的实施顺序建议

1. 先修 Benchmark 与真实 LLM/基础设施实验，使后续每项架构改动都能被可信测量；
2. 再让 Supervisor task graph、weather 节点和持久化 DAG 与真实执行一致；
3. 然后接通可恢复 Agent Transport 的单一 owner 模式，并做真实 crash/reclaim/fencing 测试；
4. 加深 Critic、Context、Router 的标注评测和成本/质量闭环；
5. 最后补全 OpenTelemetry 和文档同步。

整个顺序不需要增加任何新 Agent。成熟度来自边界正确、执行可恢复、决策可复现和证据可信，而不是角色数量。
