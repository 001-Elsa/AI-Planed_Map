/* 社交路由:分享(公开只读)/ 好友 / 排行榜 */
'use strict';

const crypto = require('crypto');
const { db } = require('../db');
const { ok, fail } = require('../util');
const { areFriends } = require('../auth');
const { jsonWithin } = require('../validate');

module.exports = function register(route) {

  /* ---- 分享 ---- */
  const SHARES_MAX = parseInt(process.env.SHARES_MAX, 10) || 50;      // 每用户分享上限
  const SHARE_TTL_DAYS = parseInt(process.env.SHARE_TTL_DAYS, 10) || 180;

  route('POST', /^\/api\/shares$/, true, async (req, res, m, body) => {
    const { type, payload } = body;
    if (!['track', 'plan'].includes(type) || payload === undefined) return fail(res, 400, '缺少分享内容');
    const payloadJson = jsonWithin(payload, 500000);
    if (!payloadJson) return fail(res, 400, '分享内容过大');
    const count = db.prepare('SELECT COUNT(*) AS c FROM shares WHERE user_id = ?').get(req.user.id).c;
    if (count >= SHARES_MAX) return fail(res, 429, `分享数量已达上限(${SHARES_MAX} 个),请先删除旧分享`);
    const token = crypto.randomBytes(8).toString('hex');
    db.prepare('INSERT INTO shares (token, user_id, type, payload) VALUES (?, ?, ?, ?)')
      .run(token, req.user.id, type, payloadJson);
    ok(res, { token });
  });
  route('GET', /^\/api\/shares$/, true, async (req, res) => {
    ok(res, db.prepare('SELECT id, token, type, created_at FROM shares WHERE user_id = ? ORDER BY id DESC').all(req.user.id));
  });
  route('DELETE', /^\/api\/shares\/(\d+)$/, true, async (req, res, m) => {
    db.prepare('DELETE FROM shares WHERE id = ? AND user_id = ?').run(Number(m[1]), req.user.id);
    ok(res);
  });
  /* 公开读取(无需登录,token 为 16 位随机 hex,不可枚举;过期视为不存在) */
  route('GET', /^\/api\/share\/([0-9a-f]{16})$/, false, async (req, res, m) => {
    const row = db.prepare(
      "SELECT s.type, s.payload, s.created_at, u.nickname FROM shares s JOIN users u ON u.id = s.user_id WHERE s.token = ? AND s.created_at >= datetime('now','localtime', ?)"
    ).get(m[1], '-' + SHARE_TTL_DAYS + ' days');
    if (!row) return fail(res, 404, '分享不存在或已过期');
    let payload = null;
    try { payload = JSON.parse(row.payload); } catch (e) { /* 损坏则返回 null */ }
    ok(res, { type: row.type, payload, nickname: row.nickname, created_at: row.created_at });
  });

  /* ---- 好友 ---- */
  route('POST', /^\/api\/friends\/request$/, true, async (req, res, m, body) => {
    const { username } = body;
    if (!username) return fail(res, 400, '请输入对方用户名');
    const target = db.prepare('SELECT id, nickname FROM users WHERE username = ?').get(String(username));
    if (!target) return fail(res, 404, '没有这个用户');
    if (target.id === req.user.id) return fail(res, 400, '不能加自己为好友');
    const exist = db.prepare(
      'SELECT id, status FROM friends WHERE (user_id = ? AND friend_id = ?) OR (user_id = ? AND friend_id = ?)'
    ).get(req.user.id, target.id, target.id, req.user.id);
    if (exist) return fail(res, 409, exist.status === 'accepted' ? '你们已经是好友了' : '已有待处理的好友请求');
    db.prepare('INSERT INTO friends (user_id, friend_id) VALUES (?, ?)').run(req.user.id, target.id);
    ok(res, { nickname: target.nickname });
  });

  route('GET', /^\/api\/friends$/, true, async (req, res) => {
    const uid = req.user.id;
    const accepted = db.prepare(
      `SELECT f.id, u.id AS uid, u.username, u.nickname FROM friends f
       JOIN users u ON u.id = CASE WHEN f.user_id = ? THEN f.friend_id ELSE f.user_id END
       WHERE f.status = 'accepted' AND (f.user_id = ? OR f.friend_id = ?)`
    ).all(uid, uid, uid);
    const incoming = db.prepare(
      `SELECT f.id, u.id AS uid, u.username, u.nickname FROM friends f
       JOIN users u ON u.id = f.user_id WHERE f.status = 'pending' AND f.friend_id = ?`
    ).all(uid);
    const outgoing = db.prepare(
      `SELECT f.id, u.id AS uid, u.username, u.nickname FROM friends f
       JOIN users u ON u.id = f.friend_id WHERE f.status = 'pending' AND f.user_id = ?`
    ).all(uid);
    ok(res, { accepted, incoming, outgoing });
  });

  route('POST', /^\/api\/friends\/respond$/, true, async (req, res, m, body) => {
    const { id, accept } = body;
    const row = db.prepare("SELECT * FROM friends WHERE id = ? AND friend_id = ? AND status = 'pending'").get(Number(id), req.user.id);
    if (!row) return fail(res, 404, '请求不存在');
    if (accept) db.prepare("UPDATE friends SET status = 'accepted' WHERE id = ?").run(row.id);
    else db.prepare('DELETE FROM friends WHERE id = ?').run(row.id);
    ok(res);
  });

  route('DELETE', /^\/api\/friends\/(\d+)$/, true, async (req, res, m) => {
    db.prepare('DELETE FROM friends WHERE id = ? AND (user_id = ? OR friend_id = ?)')
      .run(Number(m[1]), req.user.id, req.user.id);
    ok(res);
  });

  /* 好友的收藏(鉴权:必须是已接受的好友) */
  route('GET', /^\/api\/friends\/(\d+)\/favorites$/, true, async (req, res, m) => {
    const fid = Number(m[1]);
    if (!areFriends(req.user.id, fid)) return fail(res, 403, '你们还不是好友');
    const u = db.prepare('SELECT nickname FROM users WHERE id = ?').get(fid);
    ok(res, {
      nickname: u ? u.nickname : '',
      favorites: db.prepare('SELECT name, address, lng, lat, mode FROM favorites WHERE user_id = ? ORDER BY id DESC').all(fid),
    });
  });

  /* 运动排行榜(我 + 好友,SQL 聚合) */
  route('GET', /^\/api\/leaderboard$/, true, async (req, res) => {
    const uid = req.user.id;
    const days = Math.min(365, Math.max(1, parseInt(req.query.get('days'), 10) || 7));
    const friendRows = db.prepare(
      `SELECT CASE WHEN user_id = ? THEN friend_id ELSE user_id END AS fid
       FROM friends WHERE status = 'accepted' AND (user_id = ? OR friend_id = ?)`
    ).all(uid, uid, uid);
    const ids = [uid].concat(friendRows.map((r) => r.fid));
    const ph = ids.map(() => '?').join(',');
    const rows = db.prepare(
      `SELECT u.id AS uid, u.nickname, COALESCE(SUM(t.distance), 0) AS distance, COUNT(t.id) AS count
       FROM users u LEFT JOIN tracks t ON t.user_id = u.id AND t.created_at >= datetime('now','localtime', ?)
       WHERE u.id IN (${ph}) GROUP BY u.id ORDER BY distance DESC`
    ).all('-' + days + ' days', ...ids);
    ok(res, { days, rows, me: uid });
  });
};
