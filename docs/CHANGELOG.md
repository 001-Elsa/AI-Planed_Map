# 版本演进清单（v1 → v7）

> 本文档记录项目从纯前端地图到 AI 规划平台的历史。v1～v5 的 Node/SQLite、最近邻 + 2-opt 等描述是当时版本的真实实现，不代表当前 v6 正式后端；当前能力以 README、架构文档和代码为准。

---

## v7.17 — Event-Driven Dynamic Multi-Agent

- 行中事件新增 Companion → Supervisor → Replanner → Planner → Critic 显式执行链，并持久化动态工作流、任务、交接和制品；
- Replanner 只生成强类型重规划指令且没有工具权限，地图矩阵、OR-Tools/Beam Search 继续由 Planner 调用；
- 新增动态 Patch Critic：阻断必经点删除、约束冲突、陈旧版本证据和缺失 Provider 证据的替代 POI；
- PlanPatch 新增 `change_departure_time`，任何 Agent 都不能直接覆盖 PlanVersion；
- Patch 提交增加行锁、CAS、硬约束复验、唯一版本 fencing 和并发冲突回滚；高风险默认 HITL，低风险自动应用必须在 Trip 创建时显式 opt-in；

## v7.18 — Cost-aware Model Router

- 新增 Rule/Small/Strong/Deterministic 四档 ModelRouter，按任务复杂度、不确定性、风险、模型可用性和故障状态动态路由；
- Intent 接入 Rule/Small/Strong Structured Output，Critic 接入 Rule/Strong Hybrid，Companion 限定 Rule/Small；
- Supervisor、Search、Safety、Planner、Replanner 显式锁定确定性执行，OR-Tools/Beam Search 不进入模型调用链；
- Small/Strong 分别配置模型名和输入输出单价，Agent Runtime 使用实际路由价格执行预算检查；
- 路由指标记录角色、tier、风险与复杂度，高风险继续进入 Critic/HITL，ModelRouter 不改变工具和状态权限；

## v7.6 — Agent 自动评测

- `PlanningPreferences` 新增 `avoid_hiking` 与 `travel_style`，规则解析器覆盖轻松/紧凑旅行和登山规避表达；
- 规划期对明确避开爬山的候选执行确定性过滤，`relaxed` 风格提高距离与步行惩罚；
- 新增共用路线评分器：距离 40%、时间 30%、偏好 30%，默认 75 分通过；
- deadline、重复/缺失 POI、时长/步行/费用硬限制和极端距离触发 hard fail，总分强制为 0；
- Critic Review Report 写入分项、总分、公式和 hard-failure code；LLM Critic 不能覆盖服务器硬失败；
- 意图集扩充偏好正负例，新增 6 条路线 golden cases、运行时指标和 CI `evaluate_routes.py` 门禁。

## v7.5 — Agent Memory 系统

- Shared State 从“仅 TTL 过期”升级为任务终止主动删除，Companion 状态在 Trip 完成后删除，TTL 只作为异常兜底；
- PostgreSQL 长期偏好接入后续规划，仅允许显式确认的固定软偏好 Schema，不自动保存行为推断；
- 建立“本次结构化值 > 本次文本意图 > 长期偏好 > Parser 默认值”的合并优先级，并支持 `use_long_term_memory=false`；
- 新增长期偏好查询、单项撤销、隐私导出元数据与整库清除；多轮会话冻结首轮有效 Memory；
- 地点类别、环境和少排队偏好只作为通用发现请求的有界召回提示，不替代 Provider 事实；
- 新增 Memory 值校验、越权 key、当前请求覆盖、停用、撤销及短期状态清理测试与指标。

## v7.4 — Agent Tool Registry 与能力隔离

- 新增失败关闭的 `AgentToolRegistry`，统一声明能力 owner、调用模式、完整数据域和副作用；
- `AgentSpec` 分离模型可选 `allowed_tools` 与服务器内部 `allowed_internal_capabilities`，地图和 OR-Tools 不进入 LLM Tool Schema；
- Intent、Search、Planner 的真实解析/地图/求解入口以及 Companion Controller/HTTP Tool 入口接入授权检查；
- Companion Tool 仍需继续通过 Trip State、Consent、确认和预算 Policy，高影响持久化/分享操作标记为 workflow-only 且无 Agent owner；
- 新增跨角色、未知 Tool、调用模式混淆、Planner 越权数据域和 workflow-only 越权红队测试，并增加授权结果指标。

## v7.3 — 版本化 Agent Shared State

- 新增包含需求、候选、方案、评价、行中上下文和只追加历史的 `AgentSharedState`；
- Redis 与内存 RuntimeStore 增加 revision CAS，防止并行 Agent 丢失更新；
- AgentMessage 携带状态引用与 revision，陈旧或跨任务引用失败关闭；
- 为 Supervisor、Intent、Search、Planner、Critic、Companion 建立字段级读写权限；
- Search、Planner、Critic 改为从角色状态切片读取真实上游结果，Companion 可跨行中事件读取路线与上次执行摘要；
- PostgreSQL 新增最小化 `AgentSharedStateSnapshot`，完整临时状态仍由 Redis TTL 管理；
- 长期偏好继续只接受明确用户确认，正式计划和旅游历史继续由既有版本/事件表保存；
- 新增 CAS 冲突、越权写入、角色视图、伪造状态引用和 Companion 跨事件状态测试。

---

## v7.2 — Supervisor 统一通信协议

- 新增版本化 `AgentMessage` 信封和失败关闭的 `AgentMessageRouter`，统一规划链路与 Companion 行中链路；
- 通过 task/correlation/causation ID、内容哈希和幂等键建立可追踪因果链；
- 规划阶段真实输入经消息协议重新校验后消费，消息不再只是日志；
- Agent 路由实施显式白名单，禁止 Critic 绕过 Supervisor 修改 Planner，也禁止 Companion 进入规划期角色；
- `agent_messages` 扩展为规划与行中共用审计表，并保留旧 Companion 记录兼容；
- 消息审计执行敏感字段最小化和超大正文摘要化，新增越权、幂等、因果关系与脱敏测试。

---

## v7 — 隔离的三 Agent 工作流

- 固定为 Intent、Critic、Companion 三个 Agent；地图、求解器、Validator、权限和持久化继续保持确定性；
- 三个角色拥有独立 Schema、Prompt 版本、工具白名单和预算，只通过编排器传递版本化 Artifact；
- Critic 支持 off/shadow/enforce，最多触发一次仅调整软权重的重算，不能生成 POI、修改硬约束或写 PlanVersion；
- 新增 AgentWorkflowRun / AgentRun / AgentArtifact 审计链、角色延迟/状态/成本指标和前端执行阶段；
- Patch 接受复用联合求解器的完整约束评价，替换 POI 时保留原任务身份；
- 初次规划、公共交通复评与动态重规划的 CPU 求解移入线程；
- Worker 增加 processing 保留队列、显式 ack 和启动恢复，缩小硬崩溃丢失在途消息窗口；
- 新增三 Agent 隔离单测及 `backend/tests/evaluation/evaluate_agents.py` 离线质量门禁。

---

## v1 — 最小可用:六模式纯前端地图

**形态**:纯前端单页应用(index.html + style.css + app.js),无后端。

**功能**
- 🍜 吃货模式:灰色底图 + `setFeatures(['bg','road','building'])` 隐藏底图 POI 标注,只显示餐饮类 POI(高德分类码 050000),按距离列表,点击弹窗含地址/电话/距离,跳转高德导航
- 🚻 厕所模式:同上,公厕(200300)
- 🛍️ 逛街模式:同上,商场/超市/专卖店
- 🏃 跑步模式:清新绿底图,标出公园/绿道/体育场;地图点两点用 Walking 规划路线,按 6 分/公里估配速,给折返里程
- 🚴 骑行模式:点两点用 Riding 规划,显示距离与预计时长
- 📝 计划模式:每行输入一件事 → `PlaceSearch.searchNearBy` 就近匹配地点 → **最近邻 + 2-opt 求最短访问顺序**(开放路径 TSP,球面距离矩阵)→ 按用户所选出行方式(步行/骑行/驾车)逐段规划真实路线,失败段直线虚线兜底;编号站点 + 彩色分段 + 总里程总耗时

**基础设施**
- 高德 Key + 安全密钥由用户在应用内弹窗填写,存 localStorage(内存兜底)
- 视野变化(moveend)防抖 450ms 自动刷新 POI
- PWA:manifest + Service Worker 应用壳缓存 + 图标,可"添加到主屏幕"
- 移动端优先布局,宽屏自适应侧栏

---

## v2 — 前后端化:用户系统 + 数据库 + 模式扩展到 12 个

**架构变更**
- 新增**零第三方依赖后端**:Node 内置 `http`(路由+静态托管)+ `node:sqlite`(数据库)+ `crypto.scrypt`(密码哈希)。`node server.js` 单命令启动
  - 决策背景:最初选 Express + better-sqlite3,因 npm 受限改为零依赖,后固化为特色
- 建表:`users`、`sessions`(可吊销会话,30 天过期)、`favorites`、`plans`、`tracks`
- 前端拆出 `api.js`(API 封装)+ 登录/注册视图;支持**游客模式**与**后端离线降级**

**新增模式(+6)**
- 🅿️ 停车、⛽ 加油充电、🏥 救急(医院/药店/诊所/24h 药店)、🏨 酒店、⭐ 收藏(收藏集中展示)
- 底部标签栏改为可横向滑动

**功能深化**
- POI 模式:分类筛选 chips(吃货 10 类、逛街 8 类…)、关键词搜索、距离/评分双排序(吃货/酒店用 `extensions:'all'` 取评分与人均价)、信息窗 ⭐收藏 / 🚶到这去(步行路线)/ 高德导航;厕所/停车/加油/救急有「最近的一个」一键步行直达
- 跑步/骑行:两点升级为**多点连线**(逐段拼接),支持撤销;路线可保存到 `tracks` 表并回看/删除
- 计划:计划可保存/载入/删除(存 `plans` 表)

---

## v3 — 能力质变:实况记录 + 公交 + 社交雏形,模式扩展到 15 个

**新增模式(+3)**
- 🚌 公交模式:`AMap.Transfer` 换乘规划(`map.getCity` 自动识别城市),最多 3 方案对比(耗时/距离/票价/换乘线路),地铁紫/公交绿/步行虚线分色,分段换乘指引
- 📔 足迹模式:表情打卡 + 心情备注,自动以最近 POI 命名,地图足迹日记(新增 `checkins` 表)
- 📊 统计模式:跑骑总里程/次数、近 8 周运动量柱状图(SQL `strftime` 按周聚合)、足迹/收藏计数(新增 `/api/stats`)

**重量级功能**
- 🎽 **GPS 实况记录**:`watchPosition` 高精度流,<2m 抖动 / >200m 跳变双阈值过滤,实时里程/用时/配速,每公里自动落标,暂停/继续,结束存库(`is_real` 标记)或未登录导出 GPX;明确 WGS-84 vs GCJ-02 坐标系取舍
- 📝 **计划模式质变**:≤6 站改用**真实路网距离矩阵**(并发成对请求)+ **全排列精确 TSP**;出发时间 + 每站停留时长 → 逐站预计到达/离开时刻;每站办完打勾划掉
- 🌤 天气建议(`AMap.Weather`,判断是否适合运动)、📋 路线逐段文字指引、📤 GPX 导出、🌙 夜间模式(深色 UI + 高德 dark 底图,记忆偏好)

---

## v4 — 全面扩展:社交闭环 + 安全代理 + 管理后台,模式扩展到 16 个

**六个体验功能**
- ▶️ 轨迹回放:rAF 插值动画,实录轨迹按**逐点时间戳**(`path` 存 `[lng,lat,t]`)**变速回放**,显示"此刻配速",全程压缩至 8~40 秒
- ✋ 计划拖拽调序:HTML5 拖拽 + ▲▼ 按钮,按新顺序**实时重算**每段路线,可"恢复最优"
- 🔗 分享链接:`shares` 表 + 16 位随机 hex token,`/share.html?t=…` 公开只读页(地图 + 明细 + 在线回放),链接一键复制
- 🔥 热力足迹:`AMap.HeatMap` 叠加全部轨迹
- 🗣️ 语音播报:`speechSynthesis` 每公里报里程/用时/配速,可开关
- 👥 找中间点:**Weiszfeld 迭代求几何中位数**(对所有人总路程最短),吃货模式联动搜周边美食

**架构级功能**
- 🔐 **高德 Key 服务端代理**:`settings` 表存 Key + jscode;前端 `_AMapSecurityConfig.serviceHost` 指向 `/_AMapService`,服务器附加 jscode 转发 `restapi.amap.com`(高德官方生产方案),**安全密钥永不下发浏览器**;未配置自动回退本机 Key;支持环境变量注入
- 🤝 好友系统:`friends` 表(pending/accepted),请求/同意/拒绝/删除,查看好友收藏(需鉴权为好友),**7/30 天运动排行榜**(SQL 聚合)
- 🛠️ 管理后台 `/admin.html`:首个注册账号自动成为管理员;用户总览、总量统计、删除用户(外键级联清数据,`PRAGMA foreign_keys=ON`)、配置服务端 Key
- ⏰ 每周未运动站内提醒;📱 Capacitor 打包配置 + `docs/ANDROID.md`

---

## v5 — 工程化(求职版):测试、安全、DevOps、文档、前端模块化

**后端分层重构**(行为不变,由测试保障)
- `server.js`(入口/安全头/访问日志/优雅停机)+ `src/db.js`(连接/建表/迁移/settings)+ `src/auth.js`(scrypt/会话/好友关系/限流桶)+ `src/util.js`(统一响应/body 解析/分页)+ `src/static.js`(MIME/防路径穿越)+ `src/amapProxy.js` + `src/routes/{users,data,social,admin}.js`

**自动化测试**
- `test/api.test.js`:`node:test` 黑盒测试 ×21——spawn 真实服务进程(随机端口 + `DATA_DIR` 临时目录 + 环境变量收紧限流阈值),覆盖参数校验、权限隔离(A 删不掉 B 的数据)、好友鉴权、公开分享、管理员边界、**级联删除**(删用户其 token 立即 401)、限流 429、分页、分享配额、路径穿越、安全响应头
- `test/e2e.run.cjs`:Playwright 前端冒烟 ×12——模块加载零报错、注册/登录/游客/复访免登录、管理后台、服务端 Key 配置生效、分享页渲染

**安全与可观测性**
- 登录限流(按 ip+username 记**失败**,成功清零)、注册按 ip 防刷号、**登录态写接口通用限流**(每用户每分钟)、分享每用户配额(默认 50)+ 180 天过期清理
- 安全响应头(nosniff / SAMEORIGIN / Referrer-Policy)、API 访问日志(含耗时)、`/api/health`、SIGTERM 优雅停机、SQL 索引

**接口改进**
- 列表接口(favorites/plans/tracks/checkins)支持 `?limit=&offset=` 分页,总数放 `X-Total-Count` 头(响应体保持数组,向后兼容)
- 前端识别高德错误码(10003 日配额超限 / CUQPS 并发超限 / Key 平台不符等),给出明确提示而非笼统"没找到"

**前端 ES Modules 三层拆分**(与后端分层对称)
- `state.js`(单一共享状态)+ `services/`(store/api/format/algo/amap——算法与地图服务为纯函数或无 DOM 依赖)+ `ui/`(dom/auth)+ `modes/`(registry 注册表 + poi/route/plan/social 各带 activate/clearAll 生命周期)+ `main.js` 入口
- 模式切换 = 注册表驱动的生命周期:清理 → 面板显隐 → 激活

**DevOps 与文档**
- Dockerfile(node:22-alpine + HEALTHCHECK)、docker-compose(数据卷)、GitHub Actions CI(Node 22/24 矩阵:后端 CJS 语法检查 + 前端 ESM 语法检查 + API 测试;独立 e2e job)、.gitignore/.dockerignore、MIT LICENSE
- README 开源项目级重写(架构图/API 表/技术要点/已知边界);`docs/INTERVIEW.md` 面试讲解手册;本 CHANGELOG

---

## v6 — AI-Planned：FastAPI、确定性规划与伴游 Agent

**正式后端替换**

- 删除旧 Node API，正式后端迁移为 Python 3.12、FastAPI、Pydantic、SQLAlchemy 2.x 异步会话和 Alembic；
- Docker Compose / CI 使用 PostgreSQL 16 与 Redis 7；本地开发、Python 集成测试和 Playwright E2E 可使用 SQLite；
- API、Worker、数据库模型和 Provider 保持同一代码库，采用模块化单体 + 独立 Worker 进程，而不是拆分微服务。

**AI 规划边界**

- LLM 只输出严格 Pydantic 意图，不能生成 POI、写 PlanVersion 或绕过约束验证；失败时降级到 RuleBased Parser，并记录不确定约束；
- 支持持久化多轮澄清，回答会写回类型化请求，再重新召回 POI 与求解；
- 每项任务并发召回多个 Provider 候选，联合选择候选地点和访问顺序；小搜索空间精确枚举，大搜索空间优先 OR-Tools，失败时回退 Beam Search；
- 路线边正式记录 source、quality、traffic timestamp、confidence 与 fallback 标记；在线置信区间仍是启发式，不宣称已完成历史校准。

**计划版本与动态重规划**

- 新增 PlanningRun、PlanVersion、PlanPatch、DecisionAuditLog 与 IdempotencyRecord；正式计划使用不可变版本，Patch 通过 base_version 做乐观并发控制；
- Trip Session 管理状态机、Consent、短期精确定位和偏航检测；
- Agent Controller 执行有步数、Token、费用、状态、授权和工具白名单限制的 Observation → Decision → Policy → Tool 循环；
- Worker 消费高风险事件，生成待确认 Patch；用户接受并通过当前 Patch 复验后才创建 Version N+1；
- 支持延误切换交通方式、闭馆替换 POI、暴雨将室外站点替换为室内候选。

**可靠性、安全与工程化**

- Runtime Store 支持内存/Redis 计数、JSON 快照、Redis List 队列、ZSET 延迟重试、DLQ、`SET NX EX` 锁和事件发布；内存实现限制缓存条目、总字节数和计数器数量，key 洪泛时限流失败关闭；
- Session Token 只存哈希；注册密码至少 8 位并使用 scrypt；精确位置使用 Fernet 字段加密、Consent 和 TTL；新公开分享链接使用 128 bit（32 位 hex）随机 token，旧 16 位链接保持可读；
- 登录/注册限流绑定来源 IP，不能通过轮换设备 Header 绕过；登录态请求再按 Session Token 限流，并保留 IP 总额度；
- Request/Trace ID 会限制字符和长度；未匹配路由聚合为固定指标标签；API 默认 `no-store` 并限制浏览器能力；高德代理只缓存不超过独立上限的成功 JSON 响应；
- 提供 JSON 日志、Prometheus/Grafana、Ruff/Mypy/Bandit/pip-audit、PostgreSQL+Redis CI、pytest/property/contract/integration/eval/chaos/load、Playwright E2E 与 Docker build。

**v6 已知边界**

- Redis List 没有 ACK/pending reclaim，Worker 硬崩溃可能丢失在途消息；
- SSE 是最新状态快照，不是完整事件回放；
- Patch 接受路径尚未复用首次规划的全部约束检查；
- OR-Tools/Beam 搜索阶段使用固定近似代价，大规模问题不保证请求权重下的全局最优；
- 同步求解仍在 API 进程内执行；真实推送通道、托管向量检索和完整 OpenTelemetry 尚未实现。

---

## v5 数据库形态（历史，9 表）

| 表 | 用途 | 关键设计 |
|---|---|---|
| users | 账号 | username 唯一;is_admin(首个注册者=1) |
| sessions | 会话 | 随机 64 hex token,30 天过期,可吊销 |
| favorites | 收藏 | 按 user_id 隔离;坐标+名称去重 |
| plans | 出行计划 | data 存 JSON(文本/方式/出发时间/停留) |
| tracks | 运动记录 | path 存 `[[lng,lat,t]]` 逐点时间戳;is_real 区分实录 |
| checkins | 足迹打卡 | emoji + note |
| shares | 分享 | 16 hex token 公开读;每用户配额;TTL 清理 |
| friends | 好友 | 双向唯一;pending/accepted |
| settings | 服务端配置 | k-v(高德 Key/jscode) |

这是 v5 Node/SQLite 阶段的数据模型。v6 ORM 还包含规划会话、幂等记录、版本/Patch、决策审计、Trip/Agent/Consent/Location/外部数据快照等表；v6 外键删除策略也不再全部是 CASCADE，例如 `planning_runs.user_id` 使用 `SET NULL` 保留规划执行证据。
