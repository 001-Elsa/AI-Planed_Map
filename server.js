/* ===================================================================
 * 随行地图 MapGo — 服务入口(零第三方依赖)
 *
 *   node server.js            # http://localhost:3000
 *
 * 环境变量:
 *   PORT                  端口(默认 3000)
 *   DATA_DIR              数据目录(默认 ./data)
 *   AMAP_KEY / AMAP_JSCODE  服务端托管高德 Key(也可在 /admin.html 配置)
 *   RATE_LIMIT_MAX / RATE_LIMIT_WINDOW_SEC  登录限流阈值/窗口
 *
 * 架构:src/db.js(数据层) src/auth.js(认证/限流)
 *       src/routes/*(路由分组) src/static.js src/amapProxy.js
 * =================================================================== */
'use strict';

require('./src/env').loadDotEnv();

const http = require('http');
const { db, dbFile } = require('./src/db');
const { userByToken, rateAllowed, rateHit, RL_WRITE_MAX } = require('./src/auth');
const { fail, readBody, accessLog } = require('./src/util');
const { serveStatic } = require('./src/static');
const { amapProxy } = require('./src/amapProxy');

const PORT = process.env.PORT || 3000;
const TRUST_PROXY = /^(1|true|yes)$/i.test(process.env.TRUST_PROXY || '');
const ENABLE_HSTS = /^(1|true|yes)$/i.test(process.env.ENABLE_HSTS || '') || process.env.NODE_ENV === 'production';

function clientIp(req) {
  if (TRUST_PROXY) {
    const xff = String(req.headers['x-forwarded-for'] || '').split(',')[0].trim();
    if (xff) return xff;
  }
  return req.socket.remoteAddress || '?';
}

function setSecurityHeaders(res) {
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('X-Frame-Options', 'SAMEORIGIN');
  res.setHeader('Referrer-Policy', 'strict-origin-when-cross-origin');
  res.setHeader('Content-Security-Policy', [
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://webapi.amap.com https://*.amap.com",
    "connect-src 'self' https://restapi.amap.com https://*.amap.com",
    "img-src 'self' data: blob: https://*.amap.com https://webapi.amap.com",
    "style-src 'self' 'unsafe-inline' https://*.amap.com",
    "font-src 'self' data:",
    "object-src 'none'",
    "base-uri 'self'",
    "frame-ancestors 'self'",
  ].join('; '));
  if (ENABLE_HSTS) res.setHeader('Strict-Transport-Security', 'max-age=31536000; includeSubDomains');
}

/* ---- 路由注册 ---- */
const routes = [];
const route = (method, pattern, needAuth, handler) =>
  routes.push({ method, pattern, needAuth, handler });

require('./src/routes/users')(route);
require('./src/routes/data')(route);
require('./src/routes/social')(route);
require('./src/routes/admin')(route);

/* ---- 服务器 ---- */
const server = http.createServer(async (req, res) => {
  const start = Date.now();

  setSecurityHeaders(res);

  let u;
  try { u = new URL(req.url, 'http://localhost'); }
  catch (e) { res.writeHead(400); res.end('Bad Request'); return; }
  const pathname = u.pathname;
  req.ip = clientIp(req);

  /* 访问日志(仅 API 与代理,避免静态资源刷屏) */
  if (pathname.startsWith('/api/') || pathname.startsWith('/_AMapService/')) {
    res.on('finish', () => accessLog(req, res, start));
  }

  if (pathname.startsWith('/_AMapService/')) return amapProxy(req, res);
  if (!pathname.startsWith('/api/')) return serveStatic(req, res, pathname);

  req.query = u.searchParams;
  const h = req.headers.authorization || '';
  req.token = h.startsWith('Bearer ') ? h.slice(7) : '';
  req.user = userByToken(req.token);

  for (const r of routes) {
    if (r.method !== req.method) continue;
    const m = pathname.match(r.pattern);
    if (!m) continue;
    if (r.needAuth && !req.user) return fail(res, 401, req.token ? '登录已过期,请重新登录' : '未登录');
    /* 登录态写操作通用限流(每用户每分钟 RL_WRITE_MAX 次),防脚本刷库 */
    if (req.user && req.method !== 'GET') {
      const wKey = 'write|' + req.user.id;
      if (!rateAllowed(wKey, RL_WRITE_MAX)) return fail(res, 429, '操作过于频繁,请稍后再试');
      rateHit(wKey, 60 * 1000);
    }
    try {
      const body = (req.method === 'POST' || req.method === 'PUT') ? await readBody(req) : {};
      await r.handler(req, res, m, body || {});
    } catch (e) {
      console.error('[error]', req.method, pathname, e.message);
      if (!res.headersSent) fail(res, 500, e.message || '服务器错误');
    }
    return;
  }
  fail(res, 404, '接口不存在');
});

server.listen(PORT, () => {
  console.log('随行地图 MapGo 已启动: http://localhost:' + PORT);
  console.log('数据库: ' + dbFile);
});

/* ---- 优雅停机:先停止接收连接,再关数据库 ---- */
function shutdown(sig) {
  console.log(`收到 ${sig},正在优雅停机…`);
  server.close(() => {
    try { db.close(); } catch (e) { /* noop */ }
    process.exit(0);
  });
  setTimeout(() => process.exit(1), 5000).unref();   // 兜底强退
}
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));

module.exports = { server };   // 供测试引用
