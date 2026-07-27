/* 用户路由:注册 / 登录(含限流)/ 退出 / 当前用户 / 健康检查 */
'use strict';

const { db } = require('../db');
const { ok, fail } = require('../util');
const {
  hashPassword, verifyPassword, issueToken,
  rateAllowed, rateHit, rateClear, RL_REG_MAX,
} = require('../auth');

const pkg = require('../../package.json');
const bootTime = Date.now();
const ADMIN_INIT_TOKEN = process.env.ADMIN_INIT_TOKEN || '';
const REQUIRE_ADMIN_INIT_TOKEN = process.env.NODE_ENV === 'production' && process.env.ALLOW_FIRST_ADMIN !== '1';

module.exports = function register(route) {

  route('GET', /^\/api\/health$/, false, async (req, res) => {
    let dbOk = true;
    try { db.prepare('SELECT 1').get(); } catch (e) { dbOk = false; }
    ok(res, {
      status: dbOk ? 'ok' : 'degraded',
      version: pkg.version,
      uptimeSec: Math.round((Date.now() - bootTime) / 1000),
      node: process.version,
    });
  });

  route('POST', /^\/api\/register$/, false, async (req, res, m, body) => {
    const ipKey = 'reg|' + req.ip;
    if (!rateAllowed(ipKey, RL_REG_MAX)) return fail(res, 429, '操作过于频繁,请稍后再试');
    rateHit(ipKey);

    const { username, password, nickname } = body;
    if (typeof username !== 'string' || !/^[\w一-龥]{2,20}$/.test(username))
      return fail(res, 400, '用户名需为 2-20 位字母/数字/下划线/中文');
    if (typeof password !== 'string' || password.length < 6 || password.length > 64)
      return fail(res, 400, '密码长度需在 6-64 位之间');
    const nick = (typeof nickname === 'string' && nickname.trim()) ? nickname.trim().slice(0, 20) : username;
    if (db.prepare('SELECT id FROM users WHERE username = ?').get(username))
      return fail(res, 409, '用户名已被注册');

    const isFirst = db.prepare('SELECT COUNT(*) AS c FROM users').get().c === 0;
    let isAdmin = 0;
    if (isFirst) {
      if (ADMIN_INIT_TOKEN) {
        if (String(body.adminInitToken || '') !== ADMIN_INIT_TOKEN) return fail(res, 403, '管理员初始化令牌不正确');
        isAdmin = 1;
      } else if (REQUIRE_ADMIN_INIT_TOKEN) {
        return fail(res, 503, '生产环境需先配置 ADMIN_INIT_TOKEN');
      } else {
        isAdmin = 1;
      }
    }
    const info = db.prepare('INSERT INTO users (username, nickname, pass_hash, is_admin) VALUES (?, ?, ?, ?)')
      .run(username, nick, hashPassword(password), isAdmin);
    const uid = Number(info.lastInsertRowid);
    ok(res, { token: issueToken(uid), user: { id: uid, username, nickname: nick, is_admin: isAdmin } });
  });

  route('POST', /^\/api\/login$/, false, async (req, res, m, body) => {
    const { username, password } = body;
    if (!username || !password) return fail(res, 400, '请输入用户名和密码');

    const key = 'login|' + req.ip + '|' + String(username);
    if (!rateAllowed(key)) return fail(res, 429, '失败次数过多,请 15 分钟后再试');

    const u = db.prepare('SELECT * FROM users WHERE username = ?').get(String(username));
    if (!u || !verifyPassword(password, u.pass_hash)) {
      rateHit(key);                                     // 只记失败
      return fail(res, 401, '用户名或密码错误');
    }
    rateClear(key);                                     // 成功清零
    ok(res, { token: issueToken(u.id), user: { id: u.id, username: u.username, nickname: u.nickname, is_admin: u.is_admin } });
  });

  route('POST', /^\/api\/logout$/, true, async (req, res) => {
    db.prepare('DELETE FROM sessions WHERE token = ?').run(req.token);
    ok(res);
  });

  route('GET', /^\/api\/me$/, true, async (req, res) => ok(res, req.user));
};
