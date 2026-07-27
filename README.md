# 🗺️ MapGo 随行地图

> 多模式地图生活应用 —— 16 种场景化地图模式 + 完整用户/社交体系,后端**零第三方依赖**(纯 Node.js 内置模块 + SQLite),自带测试、限流、Docker 与 CI。

<!-- 部署后把在线 Demo 链接和截图放在这里,求职效果翻倍
[在线 Demo](https://your-domain.com) · ![CI](https://github.com/you/mapgo/actions/workflows/ci.yml/badge.svg)
![screenshot](docs/screenshots/food-mode.png)
-->

```
前端  原生 JS(ES Modules 三层架构:services / ui / modes)+ 高德地图 JS API 2.0 + PWA
后端  Node.js ≥ 22.5(仅内置模块:http / node:sqlite / crypto)—— 零运行时依赖,node server.js 即起
测试  node:test 黑盒接口测试 ×21 + Playwright E2E ×12,GitHub Actions CI(Node 22/24 双版本)
部署  Docker / docker-compose / 裸机单命令
```

演进历程见 [docs/CHANGELOG.md](docs/CHANGELOG.md)(v1 纯前端六模式 → v5 全栈工程化,每版详细清单)。

## ✨ 功能总览

### 16 种地图模式

| | 模式 | 说明 |
|---|---|---|
| 🍜🚻🛍️🅿️⛽🏥🏨 | POI 聚焦 ×7 | 灰色底图隐藏无关标注,只显示目标类 POI;分类筛选、关键词搜索、评分/人均、距离/评分排序、一键收藏、"到这去"步行路线、最近的一个一键直达;吃货模式附带**多人找中间点**(Weiszfeld 几何中位数选聚餐点) |
| 🏃🚴 | 跑步 / 骑行 | 多点连线路线规划 + 逐段指引;**GPS 实况记录**(实时里程/配速、每公里语音播报与落标、暂停继续);**轨迹回放**(按真实配速变速);保存云端 / 导出 GPX |
| 🚌 | 公交 | 公交/地铁换乘方案对比(耗时/距离/票价),地铁/公交/步行分色绘制 |
| 📝 | 计划 | 多任务**最短路径规划**:≤6 站真实路网距离矩阵 + 全排列精确 TSP,>6 站最近邻+2-opt;**拖拽调序实时重算**;出发时间+停留时长 → 逐站 ETA;计划保存/载入/分享 |
| 📔👥⭐📊 | 足迹 / 好友 / 收藏 / 统计 | 表情打卡日记;好友请求/排行榜/看好友收藏;收藏地图;运动统计 + 近 8 周柱状图 + **热力足迹** |

### 系统能力

- **账号体系**:注册/登录/游客模式,scrypt 加盐密码,可吊销的服务端会话(30 天),登录防爆破限流
- **社交**:好友(请求/同意/删除)、7/30 天运动排行榜、只读**分享链接**(`/share.html?t=…`,含在线回放)
- **管理后台** `/admin.html`:首个注册账号自动成为管理员;用户总览、删除用户(级联清数据)、配置服务端高德 Key
- **Key 安全代理**:高德安全密钥只存服务端,前端经 `/_AMapService` 官方代理转发,浏览器不可见
- **PWA**:可"添加到主屏幕",应用壳离线缓存;附 Capacitor 打包 Android 指南(`docs/ANDROID.md`)
- 夜间模式、天气建议、每周运动提醒

## 🚀 快速开始

```bash
# 要求 Node.js ≥ 22.5(内置 SQLite),无需 npm install
node server.js
# → http://localhost:3000
```

1. 注册第一个账号(自动成为管理员)
2. 打开 `http://localhost:3000/admin.html`,把你的高德 Key + 安全密钥配到服务端
   (在 [lbs.amap.com](https://lbs.amap.com/) 创建应用 → 添加 Key → 服务平台选「Web端(JS API)」)
3. 回到主页开始使用。数据库自动生成于 `data/mapgo.db`

Docker 方式:

```bash
docker compose up -d          # 数据持久化在 ./data
```

运行测试:

```bash
npm test                      # node:test,21 个接口用例,无需任何依赖
npm run test:e2e              # Playwright 前端冒烟 ×12(需 npm i -D playwright)
```

## 🏗️ 架构

```
浏览器(public/)                          服务器(Node 内置模块)
┌─────────────────────────┐   HTTPS   ┌──────────────────────────────┐
│ index.html  16 模式主应用 │──/api/──▶│ server.js     入口/安全头/日志 │
│ share.html  公开分享页    │           │ src/routes/   users 认证+限流 │
│ admin.html  管理后台     │           │               data  个人数据  │
│ js/app.js   地图逻辑     │           │               social 好友/分享│
│ js/api.js   API 封装     │           │               admin 管理/配置 │
│ sw.js       PWA 壳缓存   │           │ src/auth.js   scrypt/会话/限流│
└───────────┬─────────────┘           │ src/db.js     node:sqlite    │
            │ _AMapSecurityConfig     │ src/static.js 静态+防穿越     │
            │ .serviceHost            │ src/amapProxy 高德安全代理    │
            ▼                         └──────────┬───────────────────┘
   高德 JS API(script)                          │ 附加 jscode 转发
   数据请求 ──▶ /_AMapService/* ────────────────▶ restapi.amap.com
                                                 │
                                      SQLite(WAL,外键级联)
                                      users/sessions/favorites/plans/
                                      tracks/checkins/shares/friends/settings
```

**为什么零依赖?** 详见 [docs/INTERVIEW.md](docs/INTERVIEW.md) 的完整讨论。简版:部署极简(单命令)、无供应链风险、镜像小;代价是路由/解析等自己实现——它们都在 `src/` 里,每层都可读可讲。

## 🔌 API 摘要

统一响应 `{ok, data|msg}`;需登录的接口带 `Authorization: Bearer <token>`。

| 分组 | 接口 |
|---|---|
| 认证 | `POST /api/register` `POST /api/login`(限流)`POST /api/logout` `GET /api/me` |
| 个人数据 | `GET/POST/DELETE /api/favorites[/:id]`、`/api/plans[/:id]`、`/api/tracks[/:id]?kind=`、`/api/checkins[/:id]`、`GET /api/stats`;列表均支持 `?limit=&offset=` 分页,总数在 `X-Total-Count` 头 |
| 社交 | `POST /api/friends/request` `GET /api/friends` `POST /api/friends/respond` `DELETE /api/friends/:id` `GET /api/friends/:uid/favorites` `GET /api/leaderboard?days=` |
| 分享 | `GET/POST/DELETE /api/shares[/:id]`、`GET /api/share/:token`(公开) |
| 系统 | `GET /api/health` `GET /api/config`(公开)、`GET/POST /api/admin/amapkey`、`GET /api/admin/overview`、`DELETE /api/admin/users/:id`、`ANY /_AMapService/*`(代理) |

环境变量:`PORT`、`DATA_DIR`、`AMAP_KEY`/`AMAP_JSCODE`、`RATE_LIMIT_MAX`/`RATE_LIMIT_REG_MAX`/`RATE_LIMIT_WRITE_MAX`/`RATE_LIMIT_WINDOW_SEC`、`SHARES_MAX`/`SHARE_TTL_DAYS`

## 🧠 技术要点(面试速查)

1. **TSP 两级求解**:≤6 站并发构建真实路网距离矩阵(`Promise.all` 成对请求)→ 全排列精确解;>6 站 haversine 矩阵 + 最近邻 + 2-opt。支持手动拖拽调序后逐段实时重算。
2. **Key 安全**:高德 `serviceHost` 官方代理机制,jscode 仅存服务端;未配置时自动回退浏览器本地 Key,分享页同样两级回退。
3. **认证安全**:scrypt + `timingSafeEqual`;服务端会话可主动吊销(登出/删号即失效);登录按 `ip+username` 记失败限流,注册按 ip 防刷号。
4. **GPS 实录**:抖动(<2m)与跳变(>200m)双阈值过滤;逐点相对时间戳支撑**变速回放**;WGS-84/GCJ-02 坐标系取舍见 `docs/INTERVIEW.md`。
5. **数据完整性**:`PRAGMA foreign_keys=ON` + 全表 `ON DELETE CASCADE`,删用户一步清干净(有测试兜底)。
6. **测试**:后端 spawn 真实服务进程(随机端口+临时数据目录)做黑盒测试,覆盖权限隔离、级联删除、限流 429、分页、分享配额、路径穿越、安全头;前端 Playwright E2E 覆盖模块加载、注册/登录/游客/复访、管理后台、分享页。
7. **防滥用**:登录失败限流 + 注册防刷号 + 登录态写接口通用限流 + 分享每用户配额与 TTL 过期;高德配额类错误(10003/CUQPS 等)前端识别并明确提示。

已知边界(诚实清单):限流为单实例内存版(多实例需 Redis);未配 CSP;HTTPS 依赖反向代理;SQLite 适合单机,横向扩展需换 PostgreSQL(数据层已隔离在 `src/db.js`)。

## 📁 目录结构

```
mapgo/
├── server.js                 # 入口:路由装配、安全头、访问日志、优雅停机
├── src/
│   ├── db.js                 # node:sqlite 连接/建表/迁移/settings
│   ├── auth.js               # scrypt、会话、好友关系、限流桶
│   ├── util.js               # 统一响应、body 解析、访问日志
│   ├── static.js             # 静态服务(MIME + 防路径穿越)
│   ├── amapProxy.js          # /_AMapService 高德安全代理
│   └── routes/{users,data,social,admin}.js
├── public/
│   ├── index.html / share.html / admin.html
│   └── js/                   # 前端 ES Modules(与后端分层对称)
│       ├── main.js           #   入口
│       ├── state.js          #   单一共享状态
│       ├── services/         #   store / api / format / algo(TSP·Weiszfeld)/ amap(含错误码解释)
│       ├── ui/               #   dom(toast/转义/语音)/ auth(登录·Key 配置)
│       └── modes/            #   registry(模式注册表+生命周期)/ poi / route / plan / social
├── test/
│   ├── api.test.js           # 21 个后端黑盒接口测试(node:test)
│   └── e2e.run.cjs           # 12 个前端 E2E 冒烟(Playwright)
├── docs/{CHANGELOG.md, INTERVIEW.md, ANDROID.md}
├── Dockerfile / docker-compose.yml / .github/workflows/ci.yml
└── package.json              # dependencies: {} ← 真的是空的
```

## 📄 License

[MIT](LICENSE)
