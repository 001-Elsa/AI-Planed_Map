/* 管理路由:公开配置 / 总览 / 用户管理 / 服务端高德 Key */
'use strict';

const { db, getSetting, setSetting } = require('../db');
const { ok, fail } = require('../util');

function requireAdmin(req, res) {
  if (!req.user || !req.user.is_admin) { fail(res, 403, '需要管理员权限'); return false; }
  return true;
}

module.exports = function register(route) {

  /* 公开:前端启动时读取服务端 Key 配置状态 */
  route('GET', /^\/api\/config$/, false, async (req, res) => {
    const key = getSetting('amap_key');
    const hasJscode = !!getSetting('amap_jscode');
    ok(res, { amapKey: key || null, proxy: !!(key && hasJscode) });
  });

  route('GET', /^\/api\/admin\/overview$/, true, async (req, res) => {
    if (!requireAdmin(req, res)) return;
    const users = db.prepare(
      `SELECT u.id, u.username, u.nickname, u.is_admin, u.created_at,
         (SELECT COUNT(*) FROM tracks t WHERE t.user_id = u.id) AS tracks,
         (SELECT COALESCE(SUM(distance),0) FROM tracks t WHERE t.user_id = u.id) AS distance,
         (SELECT COUNT(*) FROM favorites f WHERE f.user_id = u.id) AS favorites,
         (SELECT COUNT(*) FROM checkins c WHERE c.user_id = u.id) AS checkins,
         (SELECT COUNT(*) FROM plans p WHERE p.user_id = u.id) AS plans
       FROM users u ORDER BY u.id`
    ).all();
    const totals = {
      users: users.length,
      tracks: db.prepare('SELECT COUNT(*) AS c FROM tracks').get().c,
      distance: db.prepare('SELECT COALESCE(SUM(distance),0) AS d FROM tracks').get().d,
      shares: db.prepare('SELECT COUNT(*) AS c FROM shares').get().c,
      checkins: db.prepare('SELECT COUNT(*) AS c FROM checkins').get().c,
    };
    ok(res, { users, totals });
  });

  route('DELETE', /^\/api\/admin\/users\/(\d+)$/, true, async (req, res, m) => {
    if (!requireAdmin(req, res)) return;
    const uid = Number(m[1]);
    if (uid === req.user.id) return fail(res, 400, '不能删除自己');
    db.prepare('DELETE FROM users WHERE id = ?').run(uid);   // 外键级联清其全部数据
    ok(res);
  });

  route('GET', /^\/api\/admin\/amapkey$/, true, async (req, res) => {
    if (!requireAdmin(req, res)) return;
    const key = getSetting('amap_key') || '';
    const js = getSetting('amap_jscode') || '';
    ok(res, { key, jscodeMasked: js ? js.slice(0, 4) + '****' : '', hasJscode: !!js });
  });

  route('POST', /^\/api\/admin\/amapkey$/, true, async (req, res, m, body) => {
    if (!requireAdmin(req, res)) return;
    const { key, jscode } = body;
    setSetting('amap_key', String(key || '').trim());
    if (jscode !== undefined && String(jscode).trim() !== '') setSetting('amap_jscode', String(jscode).trim());
    if (!String(key || '').trim()) setSetting('amap_jscode', '');
    ok(res);
  });
};
