# 仓库运行证据索引

本文件只索引可复现命令与产出位置，不手写伪造指标。

## CI

- Workflow: `.github/workflows/ci.yml`
- Jobs: quality / python-postgres / frontend-e2e / container
- E2E 入口: `npm run test:e2e` → `test/e2e.run.cjs`（uvicorn + 临时 SQLite）

## AI 离线评测

```bash
python backend/tests/evaluation/evaluate_intent.py
```

质量门禁失败会使进程以非零退出码结束。

## 混沌 / 故障注入

```bash
python backend/tests/chaos/run_chaos.py
```

覆盖：内存队列“重启丢失”、分布式锁竞争、重试耗尽进 DLQ、429 退避时序。

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
