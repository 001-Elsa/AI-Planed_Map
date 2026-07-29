# MapGo 威胁模型（初版）

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
| 重放产生重复规划/费用 | 用户隔离的幂等键、指纹和 TTL；Runtime Store 分布式锁 | Redis 锁指标与告警 |
| 旧 Patch 覆盖新版本 | base version 乐观锁；行程级 mutate 锁 | 数据库行级锁 |
| 上游失败被包装成精确 ETA | 每条边带 fallback 和 confidence；启发式/残差区间 | 前端统一可信度策略 |
| 密钥进入镜像或前端 | 仅环境变量注入，代理不返回 jscode | Secret Manager |
| 未授权读取他人计划 | 所有版本/Patch 查询绑定 user_id | 租户级 RLS |
| 持续保存精确位置 | 显式 Consent、加密字段、短期 TTL、可导出/清除 | 更细粒度设备级授权 |

## 明确禁止

- 未经同意共享实时位置；
- 将一次行为自动保存为长期偏好；
- Agent 绕过 Plan Patch 删除站点或增加费用；
- 在日志中记录 Session Token、模型密钥或精确轨迹正文。
