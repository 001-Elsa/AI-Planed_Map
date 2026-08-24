# 仓库运行证据索引

本文件只索引可复现命令与产出位置，不手写伪造指标。

## CI

- Workflow: `.github/workflows/ci.yml`
- Jobs: quality / python-postgres / frontend-e2e / container
- E2E 入口: `npm run test:e2e` → `test/e2e.run.cjs`（uvicorn + 临时 SQLite）

## Python 测试与覆盖率

```bash
python -m pytest -c backend/pytest.ini --cov --cov-report=term-missing
```

测试数量和覆盖率会随代码变化，以命令当次输出与 CI artifact `coverage.xml` 为准，不在文档中固化易过期数字。

## AI 离线评测

```bash
python backend/tests/evaluation/evaluate_intent.py
python backend/tests/evaluation/evaluate_agents.py
python backend/tests/evaluation/evaluate_routes.py
```

三个门禁分别覆盖意图字段、Agent 隔离/审阅和确定性路线评分；任一失败都会使进程以非零退出码结束。

## 混沌 / 故障注入

```bash
python backend/tests/chaos/run_chaos.py
```

覆盖：内存队列“重启丢失”、分布式锁竞争、重试耗尽进 DLQ、429 退避时序。该脚本验证故障处理机制，不证明 Redis List 具备 ACK 或在途消息恢复语义。

## 压测

```bash
python backend/tests/load/planning_load.py --token <token> --requests 100 --concurrency 20
```

输出 P50/P95/P99 与 throughput_rps；结果以当次实测为准。

## 可观测性

```bash
docker compose --profile observability up
curl http://localhost:3000/metrics
```

Grafana provisioning 位于 `infrastructure/`。
