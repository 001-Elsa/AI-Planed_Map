# 1–2 分钟演示与运行证据

以下流程只依赖本仓库的 Mock Map / Mock Weather，可离线复现；接入真实地图或 LLM 时，安全边界和审批流程不变。

## 演示脚本（约 90 秒）

1. 在规划页输入：“明天下午从酒店出发，先去博物馆，再去商场，尽量少走路”。展示 LLM/规则解析出的约束、候选 POI、联合求解结果和 Version 1。
2. 对同名 POI 选择一个候选，并补充预约时间、素食忌口或步行上限。再次提交后展示最终 `intent`、候选与站点已变化：选择的 POI 被当作硬选择，预约时间进入时间窗，忌口进入餐饮召回关键词。
3. 创建伴游并开始行程。模拟 `ScheduleDelayDetected` 或 `WeatherAlertReceived`。
4. 展示 AgentRun：真实 LLM 配置或规则/脚本化决策器依次读取 Trip State、位置或天气；每一次调用都经 Policy 审查并写入 AgentToolCall。
5. 展示 SSE 最新状态快照中的 `pending` Patch。延误方案可切换交通方式；强降雨方案把室外站点替为“室内”候选。页面展示调整前后时间、距离、费用、交通方式与约束冲突。
6. 拒绝时 Version 仍是 V1；确认时重新验算并生成 V2。强调 Agent 从未直接写入正式计划。

## 可复现验证

```bash
python -m pytest -c backend/pytest.ini backend/tests/integration/test_agent_replanning.py
python backend/tests/evaluation/evaluate_intent.py
python backend/tests/evaluation/evaluate_routes.py
```

第一条使用脚本化 `AgentDecision` 验证结构化工具循环、非法工具 Policy 拒绝、调用步数上限、Worker 锁竞争、重复事件幂等、自动 pending Patch、交通方式变更、天气替换室外 POI，以及确认/拒绝的 Version 行为；它不等价于真实在线 LLM 质量评测。

评测命令输出已提交意图/路线数据集的实际指标并在低于门槛时失败；路线分数使用固定 40/30/30 公式和 hard-fail 规则，不是虚构的“模型分数”。

## 真实压测（不要提交伪造数字）

先在另一终端启动服务并登录拿到 Token：

```bash
python -m uvicorn backend.app.main:app --port 3000
python backend/tests/load/planning_load.py --token <TOKEN> --requests 100 --concurrency 20
```

脚本会输出当前机器、当前 Provider 配置下实测的 `throughput_rps` 和 p50/p95/p99。建议将该 JSON 与硬件、并发、Mock/真实 Provider 状态一同附在 release 或面试材料中，而不要把一次本地结果泛化为生产 QPS。

### 历史本地基线（2026-07-29，仅供回归对照）

Windows 本地进程、SQLite、Mock Map/Weather、RuleBased Parser，20 个请求、并发 5 的一次实测输出如下：

```json
{
  "successes": 20,
  "errors": 0,
  "throughput_rps": 19.1,
  "latency_ms": {"mean": 248.4, "p50": 196.9, "p95": 476.4, "p99": 476.4, "max": 478.3}
}
```

这是开发机 Mock 配置的基线，不代表生产或真实地图 Provider 的吞吐量。

## 录屏建议

录制时使用上述 6 步，保留 AgentRun、Patch 影响对比和 V1→V2 的画面即可。成片控制在 90 秒左右，上传后将 GIF/视频链接替换到 README 的本节链接旁。
