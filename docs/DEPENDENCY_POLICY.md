# Dependency Policy

MapGo v6 的正式后端运行时是 Python 3.12 / FastAPI，不再使用历史 Node.js / `node:sqlite` 服务。生产 Python 依赖固定在 `backend/requirements.txt`，镜像通过该文件安装依赖；新增、升级或删除运行时依赖时，必须在同一变更中更新该文件，并通过 Ruff、Mypy、Bandit、`pip-audit`、测试和 Docker build。

## Python 依赖

- Web/API：FastAPI、Starlette、Uvicorn、Pydantic；
- 数据库：SQLAlchemy、Alembic、asyncpg；本地/E2E 可使用 aiosqlite；
- 基础设施：httpx、redis、cryptography；
- 求解：OR-Tools；
- 测试：pytest、pytest-asyncio、Hypothesis、pytest-cov。

`backend/requirements.txt` 当前使用精确版本，CI 通过 `pip-audit` 检查已知漏洞。升级依赖应单独提交或在变更说明中列出兼容性影响；数据库驱动、Pydantic、SQLAlchemy、OR-Tools 和 protobuf 升级必须运行完整测试与 Alembic 检查。

## JavaScript 依赖

浏览器运行时代码位于 `public/`，使用原生 ES Modules，没有生产 npm 依赖。Node.js 只用于 Playwright E2E；`package.json` 中的 Playwright 是 devDependency，因此部署 API/Worker 不需要 `npm install`，运行 `npm run test:e2e` 才需要。

Node.js 版本下限以 `package.json#engines` 为准。若未来加入生产 JavaScript 依赖，应同时提交 lockfile，并把 CI 从 `npm install` 切换为 `npm ci`。当前仓库尚未提交 lockfile，所以 Playwright 的传递依赖并非完全可复现；这是已知的供应链技术债，不应描述为“零依赖项目”。

## 外部服务与镜像

PostgreSQL、Redis、Prometheus 和 Grafana 通过 `docker-compose.yml` 的固定主版本镜像运行。升级镜像时应验证 migration upgrade/downgrade、Redis 重试/锁行为、健康检查和 Grafana provisioning。

## 禁止事项

- 不把真实密钥、Token 或本地 `.env` 提交到仓库；
- 不在未跑审计和测试的情况下批量升级依赖；
- 不把测试依赖误写成生产运行必需；
- 不再引用已删除的 Node 后端文件、`node:sqlite` 或旧 CJS API 测试。
