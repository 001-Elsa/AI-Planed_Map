# MapGo 威胁模型（当前实现）

## 高价值资产

- 精确位置和轨迹；
- Session Token、地图与模型密钥；
- 正式行程、用户授权和长期偏好；
- Agent 工具输入输出与决策审计。

## 主要威胁与当前控制

| 威胁 | 当前控制 | 后续控制 |
|---|---|---|
| 模型生成虚假 POI | POI 只接受 Map Provider 返回值 | Provider 签名快照与新鲜度 SLA |
| Prompt 注入修改正式计划 | LLM 无数据库写权限；Patch 需确认和验证 | 工具参数污点标记 |
| Agent 伪造身份或跨角色发消息 | AgentMessage sender/receiver/type/artifact 四元组路由白名单，接收端 Schema 复验并失败关闭 | 服务间签名和独立进程身份 |
| Intent 绕过 Supervisor 固定流转到 Search | 移除 `Intent -> Search` 协议路由；Search 只接受 Supervisor 调度后的 intent artifact | 独立服务身份签名 |
| Agent 消息重放或正文篡改 | task/correlation/causation ID、内容哈希、幂等键；工作流内去重和数据库唯一约束 | 分布式 Inbox/Outbox |
| Agent 消息携带敏感数据进入审计 | 持久化前脱敏坐标、密钥和原始文本；超大正文仅留字段/大小摘要 | 字段级数据分类策略自动化 |
| 并行 Agent 覆盖或绕过 revision 篡改状态 | Shared State revision + 原子 CAS + 状态正文哈希；过期 revision、跨任务引用和哈希不一致均失败关闭 | 高并发场景引入事件溯源/CRDT |
| Agent 越权读写共享状态 | 每个角色独立读切片和写字段白名单；变更历史只保存字段与哈希 | 独立服务身份与策略引擎 |
| Prompt 注入诱导 Agent 调用其他角色工具 | Tool Registry 对角色、调用模式和完整数据域失败关闭；内部地图/OR-Tools 能力不进入 LLM Tool Schema | 独立进程身份、网络策略和每服务独立凭据 |
| 内部 Capability 被伪装成模型 Tool | `internal_stage` 与 `agent_callable` 模式不可互换，执行入口二次授权并记录拒绝指标 | 独立 Capability Broker 服务 |
| 隐式行为被长期画像 | 长期偏好只接受显式确认和固定 Schema；Trip/定位/Critic 结果不会自动写入 Memory | 偏好保留期限和定期确认 |
| 长期偏好覆盖本次明确要求 | 当前结构化值和相关文本优先；支持单次关闭 Memory；多轮会话冻结首轮值 | 更细粒度 UI 来源标记 |
| Redis 任务上下文残留 | 所有规划终止路径主动删除，Trip 完成删除；TTL 仅作为崩溃兜底 | Redis keyspace 删除延迟告警 |
| 路线高平均分掩盖硬约束失败 | deadline、重复 POI、步行/费用/时长等硬失败一票否决并将总分置零 | 扩充真实 Provider 回放集 |
| LLM Critic 给违规路线高分 | 服务器确定性评分器覆盖 LLM 的硬失败结论，线上/离线复用同一实现 | 双模型抽样 Judge 仅用于软质量 |
| 评测集过拟合或缺少负例 | 版本化正/负/红队 case，CI 检查字段准确率、硬失败检出率和最低好路线分数 | 按城市/人群分层数据集和盲测集 |
| Redis 临时状态泄露精确位置 | 任务级 key、短 TTL、不进入持久化快照；生产 Redis 不对公网开放 | Redis TLS、ACL、字段级加密 |
| 重放产生重复规划/费用 | 数据库幂等记录、用户隔离的 key、请求指纹和 TTL | 处理中记录恢复、幂等指标与告警 |
| 旧 Patch 覆盖新版本 | base version 乐观锁；行程级 mutate 锁 | 数据库行级锁 |
| 上游失败被包装成精确 ETA | 每条边带 fallback 和 confidence；启发式/残差区间 | 前端统一可信度策略 |
| 上游搜索失败被伪装成真实 POI | Supervisor 只允许使用 provider-verified POI 缓存；缓存结果降低 confidence 并标记 `cache:` source；无缓存时进入澄清 | 跨实例 Redis/数据库级恢复缓存与过期策略 |
| 密钥进入镜像或前端 | 仅环境变量注入，代理不返回 jscode | Secret Manager |
| 攻击者轮换设备 Header 绕过认证限流 | 登录/注册预算只绑定来源 IP，不把客户端设备 Header 作为新预算 key；另有更宽松的 IP 总额度 | 反向代理可信 IP 链配置、账号维度失败预算与渐进退避 |
| 随机路径或自定义追踪头造成内存/日志膨胀 | 未匹配路由统一使用 `__unmatched__` 指标标签；Request/Trace ID 仅接受长度不超过 100 的安全字符 | 指标标签持续审计与异常流量告警 |
| 内存 RuntimeStore 遭缓存/计数器 key 洪泛 | JSON 缓存限制条目数和总字节数并清理/淘汰；计数器达到上限后失败关闭，避免重置活跃限流预算 | 生产强制 Redis、内存与 key 数量监控 |
| 公开分享 token 被枚举 | 新链接使用 128 bit capability token；旧 64 bit token 只为兼容读取；分享有 180 天读取 TTL | 主动吊销、访问审计与旧 token 迁移 |
| API 响应被中间缓存或浏览器获得多余能力 | `/api/` 默认 `Cache-Control: no-store`；限制摄像头/麦克风，定位仅允许同源 | 按接口细化 CSP 与 Permissions Policy |
| 未授权读取他人计划 | 所有版本/Patch 查询绑定 user_id | 租户级 RLS |
| 持续保存精确位置 | 显式 Consent、加密字段、短期 TTL、可导出/清除 | 更细粒度设备级授权 |
| Worker 在 `BRPOP` 后硬崩溃导致事件丢失 | 数据库保留 TripEvent；捕获到的异常会延迟重试并进入 DLQ | Redis Streams/消息代理 ACK、pending reclaim、outbox/inbox |
| 分布式锁 TTL 到期后出现并发执行 | token 校验释放；事件状态和 source_event_id 去重 | 锁续租或 fencing token；数据库唯一约束 |
| SSE 重连遗漏中间事件 | 最新状态快照 + Last-Event-ID 去重 | 持久化事件序列与按 ID 回放 |
| Patch 路径遗漏部分首次规划约束 | deadline、最晚返回、步行和总费用复验 | 统一复用完整 Constraint Validator |
| 高德大响应挤占代理缓存 | 流式读取时限制总响应体；仅缓存成功、合法且不超过独立缓存上限的 JSON 响应 | 项目级配额、缓存命中率和大响应告警 |

## 新增 Agent 加固控制

- Critic `shadow -> enforce` 不靠人工感觉切换：管理员 readiness 报告要求 shadow 样本数、fallback 率、blocking 率、预算超限率和 p95 延迟全部达标，才建议灰度 enforce。
- Agent 审计数据最小化：Artifact、Run summary、Message structured payload 和 ToolCall 摘要持久化前会移除密钥、原始用户文本和精确坐标。
- 红队回归覆盖角色越权：Critic 不能夹带硬约束修改；Companion 即使返回已注册但非本角色工具，也会被 Controller 和 HTTP tool endpoint 拒绝。
- Agent Message Router 二次验证关键 Artifact：Critic 即使走合法回传路由也不能在 Review Report 中夹带硬约束；正文或幂等签名被篡改会直接拒绝。
- Shared State 写入按角色和字段授权，并要求正确 revision；PostgreSQL 只落最小化摘要和状态哈希，完整候选与精确坐标留在有 TTL 的运行时状态中。
- Tool Registry 将能力注册与行程状态 Policy 分层：Planner 只能访问路线数据域，不能读取用户长期偏好或精确定位；Companion 不能调用 `search_poi`、路线矩阵或优化器。
- 红队回归覆盖未知 Tool、跨角色调用、内部/模型调用模式混淆、Planner 请求用户数据域以及 Agent 尝试执行 workflow-only 高影响操作。
- Memory 回归覆盖未确认写入、任意 key/value 注入、当前请求覆盖、单次停用、逐项撤销和任务结束后 RuntimeStore 无残留。
- Evaluation 回归覆盖意图偏好字段、截止时间、重复景点、超远路线、超时路线和偏好不匹配；评分公式与 hard-fail 契约由单测固定。

## 明确禁止

- 未经同意共享实时位置；
- 将一次行为自动保存为长期偏好；
- Agent 绕过 Plan Patch 删除站点或增加费用；
- 在日志中记录 Session Token、模型密钥或精确轨迹正文。
