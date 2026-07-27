/* ===================================================================
 * 随行地图 MapGo — 后端接口测试(node:test,零第三方依赖)
 *
 *   npm test          # 等价于 node --test test/
 *
 * 策略:spawn 一个真实服务进程(临时数据目录 + 随机端口),
 *       用内置 fetch 走完整 HTTP 栈做黑盒测试。
 * =================================================================== */
'use strict';

const { test, before, after } = require('node:test');
const assert = require('node:assert');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PORT = 3900 + Math.floor(Math.random() * 100);
const BASE = `http://127.0.0.1:${PORT}`;
let proc = null;
let tmpDir = null;

function api(method, url, { token, body } = {}) {
  return fetch(BASE + '/api' + url, {
    method,
    headers: Object.assign(
      { 'Content-Type': 'application/json' },
      token ? { Authorization: 'Bearer ' + token } : {}
    ),
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then(async (r) => ({ status: r.status, json: await r.json().catch(() => null) }));
}

async function waitReady(tries = 50) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(BASE + '/api/health');
      if (r.ok) return;
    } catch (e) { /* 未就绪 */ }
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error('服务未能在超时内启动');
}

before(async () => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'mapgo-test-'));
  proc = spawn(process.execPath, [path.join(__dirname, '..', 'server.js')], {
    env: Object.assign({}, process.env, {
      PORT: String(PORT),
      DATA_DIR: tmpDir,
      RATE_LIMIT_MAX: '3',            // 便于测试登录限流
      RATE_LIMIT_WINDOW_SEC: '60',
      RATE_LIMIT_WRITE_MAX: '1000',   // 写限流放宽,避免干扰其它用例
      ENABLE_HSTS: '1',
      AMAP_PROXY_RATE_MAX: '2',
      SHARES_MAX: '2',                // 便于测试分享配额
    }),
    stdio: 'ignore',
  });
  await waitReady();
});

after(() => {
  if (proc) proc.kill('SIGTERM');
  try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch (e) { /* noop */ }
});

/* 贯穿用例的状态 */
const S = {};

test('健康检查', async () => {
  const r = await api('GET', '/health');
  assert.equal(r.status, 200);
  assert.equal(r.json.data.status, 'ok');
});

test('注册:参数校验', async () => {
  assert.equal((await api('POST', '/register', { body: { username: 'a', password: '123456' } })).status, 400);
  assert.equal((await api('POST', '/register', { body: { username: 'elsa', password: '123' } })).status, 400);
});

test('注册:首个用户自动成为管理员', async () => {
  const r = await api('POST', '/register', { body: { username: 'elsa', password: 'secret1', nickname: '艾莎' } });
  assert.equal(r.status, 200);
  assert.equal(r.json.data.user.is_admin, 1);
  S.admin = r.json.data.token;
  S.adminId = r.json.data.user.id;
});

test('注册:第二个用户不是管理员;重名被拒', async () => {
  const r = await api('POST', '/register', { body: { username: 'anna', password: 'secret2', nickname: '安娜' } });
  assert.equal(r.json.data.user.is_admin, 0);
  S.anna = r.json.data.token;
  S.annaId = r.json.data.user.id;
  assert.equal((await api('POST', '/register', { body: { username: 'anna', password: 'secret2' } })).status, 409);
});

test('登录:正确/错误密码', async () => {
  assert.equal((await api('POST', '/login', { body: { username: 'elsa', password: 'secret1' } })).status, 200);
  assert.equal((await api('POST', '/login', { body: { username: 'elsa', password: 'wrong-1' } })).status, 401);
});

test('限流:默认不信任客户端伪造的 X-Forwarded-For', async () => {
  for (let i = 0; i < 3; i++) {
    const r = await fetch(BASE + '/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Forwarded-For': '203.0.113.' + i },
      body: JSON.stringify({ username: 'ghost-user', password: 'bad' }),
    });
    assert.equal(r.status, 401);
  }
  const blocked = await fetch(BASE + '/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Forwarded-For': '203.0.113.99' },
    body: JSON.stringify({ username: 'ghost-user', password: 'bad' }),
  });
  assert.equal(blocked.status, 429);
});

test('鉴权:无 token 401,未知路由 404', async () => {
  assert.equal((await api('GET', '/me')).status, 401);
  assert.equal((await api('GET', '/no-such-route')).status, 404);
});

test('收藏:增删查 + 越权隔离', async () => {
  const add = await api('POST', '/favorites', { token: S.anna, body: { name: '咖啡店', address: 'X街', lng: 116.4, lat: 39.9, mode: 'food' } });
  assert.equal(add.status, 200);
  const fid = add.json.data.id;
  /* 重复收藏被拒 */
  assert.equal((await api('POST', '/favorites', { token: S.anna, body: { name: '咖啡店', lng: 116.4, lat: 39.9 } })).status, 409);
  /* 他人删除不生效(按 user_id 过滤) */
  await api('DELETE', '/favorites/' + fid, { token: S.admin });
  const list = await api('GET', '/favorites', { token: S.anna });
  assert.equal(list.json.data.length, 1);
  /* 本人可删 */
  await api('DELETE', '/favorites/' + fid, { token: S.anna });
  assert.equal((await api('GET', '/favorites', { token: S.anna })).json.data.length, 0);
});

test('运动记录:入库与 kind 过滤', async () => {
  const r = await api('POST', '/tracks', { token: S.admin, body: { kind: 'run', name: '晨跑', distance: 5000, duration: 1800, real: true, path: [[116.4, 39.9, 0], [116.41, 39.91, 900]] } });
  assert.equal(r.status, 200);
  await api('POST', '/tracks', { token: S.admin, body: { kind: 'ride', name: '骑行', distance: 10000, duration: 2400, path: [[116.4, 39.9], [116.5, 39.95]] } });
  assert.equal((await api('GET', '/tracks?kind=run', { token: S.admin })).json.data.length, 1);
  assert.equal((await api('GET', '/tracks', { token: S.admin })).json.data.length, 2);
  /* 非法 kind 被拒 */
  assert.equal((await api('POST', '/tracks', { token: S.admin, body: { kind: 'fly', name: 'x', distance: 1, path: [] } })).status, 400);
});

test('数据校验:坐标范围、有限数与轨迹结构', async () => {
  assert.equal((await api('POST', '/favorites', { token: S.admin, body: { name: 'bad', lng: 999, lat: 39 } })).status, 400);
  assert.equal((await api('POST', '/checkins', { token: S.admin, body: { name: 'bad', lng: 116, lat: -99 } })).status, 400);
  assert.equal((await api('POST', '/tracks', { token: S.admin, body: { kind: 'run', name: 'bad', distance: -1, path: [[116, 39], [116.1, 39.1]] } })).status, 400);
  assert.equal((await api('POST', '/tracks', { token: S.admin, body: { kind: 'run', name: 'bad', distance: 1, path: [[116, 39], [999, 39]] } })).status, 400);
  const tooLarge = 'x'.repeat(200001);
  assert.equal((await api('POST', '/plans', { token: S.admin, body: { name: 'big', data: { tooLarge } } })).status, 400);
});

test('统计:byKind 聚合正确', async () => {
  const r = await api('GET', '/stats', { token: S.admin });
  const run = r.json.data.byKind.find((k) => k.kind === 'run');
  assert.equal(run.distance, 5000);
  assert.equal(run.realCount, 1);
});

test('好友:请求→接受→好友收藏可见性', async () => {
  /* 边界:加自己/加不存在的人 */
  assert.equal((await api('POST', '/friends/request', { token: S.admin, body: { username: 'elsa' } })).status, 400);
  assert.equal((await api('POST', '/friends/request', { token: S.admin, body: { username: 'nobody' } })).status, 404);

  /* 未成为好友前,看对方收藏被拒 */
  assert.equal((await api('GET', '/friends/' + S.annaId + '/favorites', { token: S.admin })).status, 403);

  const req = await api('POST', '/friends/request', { token: S.admin, body: { username: 'anna' } });
  assert.equal(req.status, 200);
  /* 重复请求被拒 */
  assert.equal((await api('POST', '/friends/request', { token: S.admin, body: { username: 'anna' } })).status, 409);

  const incoming = (await api('GET', '/friends', { token: S.anna })).json.data.incoming;
  assert.equal(incoming.length, 1);
  await api('POST', '/friends/respond', { token: S.anna, body: { id: incoming[0].id, accept: true } });

  assert.equal((await api('GET', '/friends', { token: S.admin })).json.data.accepted.length, 1);
  assert.equal((await api('GET', '/friends/' + S.annaId + '/favorites', { token: S.admin })).status, 200);
});

test('排行榜:包含我和好友,按里程排序', async () => {
  const r = await api('GET', '/leaderboard?days=7', { token: S.anna });
  assert.equal(r.json.data.rows.length, 2);
  assert.equal(r.json.data.rows[0].nickname, '艾莎');   // 15km > 0km
});

test('分享:生成后可公开读取,坏 token 404', async () => {
  const c = await api('POST', '/shares', { token: S.admin, body: { type: 'track', payload: { name: '晨跑', distance: 5000 } } });
  assert.equal(c.status, 200);
  const token = c.json.data.token;
  const pub = await fetch(BASE + '/api/share/' + token).then((r) => r.json());
  assert.equal(pub.data.payload.name, '晨跑');
  assert.equal(pub.data.nickname, '艾莎');
  assert.equal((await fetch(BASE + '/api/share/0000000000000000')).status, 404);
});

test('分享:超过每用户上限返回 429', async () => {
  /* SHARES_MAX=2,上一个用例已建 1 个 */
  assert.equal((await api('POST', '/shares', { token: S.admin, body: { type: 'plan', payload: {} } })).status, 200);
  const over = await api('POST', '/shares', { token: S.admin, body: { type: 'plan', payload: {} } });
  assert.equal(over.status, 429);
  /* 删一个后恢复 */
  const list = await api('GET', '/shares', { token: S.admin });
  await api('DELETE', '/shares/' + list.json.data[0].id, { token: S.admin });
  assert.equal((await api('POST', '/shares', { token: S.admin, body: { type: 'plan', payload: {} } })).status, 200);
});

test('分页:limit/offset 与 X-Total-Count', async () => {
  for (let i = 0; i < 3; i++) {
    await api('POST', '/checkins', { token: S.admin, body: { name: '点' + i, lng: 116 + i, lat: 39, emoji: '📍' } });
  }
  const r = await fetch(BASE + '/api/checkins?limit=2&offset=1', { headers: { Authorization: 'Bearer ' + S.admin } });
  const j = await r.json();
  assert.equal(j.data.length, 2);
  assert.equal(r.headers.get('x-total-count'), '3');
  assert.equal(j.data[0].name, '点1');   // DESC 排序,offset=1 跳过最新的“点2”
});

test('管理员:权限隔离与总览', async () => {
  assert.equal((await api('GET', '/admin/overview', { token: S.anna })).status, 403);
  const r = await api('GET', '/admin/overview', { token: S.admin });
  assert.equal(r.json.data.totals.users, 2);
});

test('管理员:配置服务端 Key 后 /config 生效', async () => {
  await api('POST', '/admin/amapkey', { token: S.admin, body: { key: 'TESTKEY', jscode: 'TESTJS' } });
  const cfg = await api('GET', '/config');
  assert.equal(cfg.json.data.amapKey, 'TESTKEY');
  assert.equal(cfg.json.data.proxy, true);
});

test('管理员:删除用户级联清数据', async () => {
  const tmp = await api('POST', '/register', { body: { username: 'temp1', password: 'secret9' } });
  const tid = tmp.json.data.user.id;
  await api('POST', '/favorites', { token: tmp.json.data.token, body: { name: 'x', lng: 1, lat: 1 } });
  /* 不能删自己 */
  assert.equal((await api('DELETE', '/admin/users/' + S.adminId, { token: S.admin })).status, 400);
  assert.equal((await api('DELETE', '/admin/users/' + tid, { token: S.admin })).status, 200);
  /* 被删用户的 token 立即失效(sessions 级联) */
  assert.equal((await api('GET', '/me', { token: tmp.json.data.token })).status, 401);
});

test('限流:连续失败登录触发 429,成功后清零', async () => {
  const bad = { username: 'anna', password: 'nope-0' };
  for (let i = 0; i < 3; i++) {
    assert.equal((await api('POST', '/login', { body: bad })).status, 401);
  }
  assert.equal((await api('POST', '/login', { body: bad })).status, 429);
  /* 正确密码也被限流挡住是预期(同 key);其他用户不受影响 */
  assert.equal((await api('POST', '/login', { body: { username: 'elsa', password: 'secret1' } })).status, 200);
});

test('登出:token 失效', async () => {
  const login = await api('POST', '/login', { body: { username: 'elsa', password: 'secret1' } });
  const t = login.json.data.token;
  await api('POST', '/logout', { token: t });
  assert.equal((await api('GET', '/me', { token: t })).status, 401);
});

test('静态服务:路径穿越被拦截', async () => {
  assert.equal((await fetch(BASE + '/')).status, 200);
  /* fetch 会先规范化 URL,双重防线下最终应为 403(服务端拦截)或 404(已被归一化),绝不能 200 */
  for (const p of ['/%2e%2e/server.js', '/..%2fserver.js', '/%2e%2e%2fserver.js']) {
    const st = (await fetch(BASE + p)).status;
    assert.ok(st === 403 || st === 404, p + ' → ' + st);
  }
});

test('高德代理:限制路径并限流', async () => {
  assert.equal((await fetch(BASE + '/_AMapService/not-v3')).status, 403);
  assert.equal((await fetch(BASE + '/_AMapService/also-not-v3')).status, 403);
  assert.equal((await fetch(BASE + '/_AMapService/nope')).status, 429);
});

test('安全响应头存在', async () => {
  const r = await fetch(BASE + '/api/health');
  assert.equal(r.headers.get('x-content-type-options'), 'nosniff');
  assert.equal(r.headers.get('x-frame-options'), 'SAMEORIGIN');
  assert.ok(r.headers.get('content-security-policy').includes("object-src 'none'"));
  assert.ok(r.headers.get('strict-transport-security').includes('max-age=31536000'));
});
