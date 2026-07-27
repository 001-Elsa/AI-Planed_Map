'use strict';

const { db } = require('../db');
const { ok, fail, pageParams, okPage } = require('../util');
const {
  cleanPath,
  cleanText,
  jsonWithin,
  validDistance,
  validDuration,
  validLngLat,
} = require('../validate');

module.exports = function register(route) {
  route('GET', /^\/api\/favorites$/, true, async (req, res) => {
    const { limit, offset } = pageParams(req);
    const total = db.prepare('SELECT COUNT(*) AS c FROM favorites WHERE user_id = ?').get(req.user.id).c;
    okPage(res, db.prepare('SELECT * FROM favorites WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?').all(req.user.id, limit, offset), total);
  });

  route('POST', /^\/api\/favorites$/, true, async (req, res, m, body) => {
    const { name, address, lng, lat, mode } = body;
    const nm = cleanText(name, 100);
    if (!nm || !validLngLat(lng, lat)) return fail(res, 400, '缺少有效地点信息');
    const dup = db.prepare('SELECT id FROM favorites WHERE user_id = ? AND name = ? AND ABS(lng - ?) < 1e-6 AND ABS(lat - ?) < 1e-6')
      .get(req.user.id, nm, lng, lat);
    if (dup) return fail(res, 409, '已经收藏过啦');
    const info = db.prepare('INSERT INTO favorites (user_id, name, address, lng, lat, mode) VALUES (?, ?, ?, ?, ?, ?)')
      .run(req.user.id, nm, cleanText(address || '', 200), lng, lat, cleanText(mode || '', 30));
    ok(res, { id: Number(info.lastInsertRowid) });
  });

  route('DELETE', /^\/api\/favorites\/(\d+)$/, true, async (req, res, m) => {
    db.prepare('DELETE FROM favorites WHERE id = ? AND user_id = ?').run(Number(m[1]), req.user.id);
    ok(res);
  });

  route('GET', /^\/api\/plans$/, true, async (req, res) => {
    const { limit, offset } = pageParams(req);
    const total = db.prepare('SELECT COUNT(*) AS c FROM plans WHERE user_id = ?').get(req.user.id).c;
    okPage(res, db.prepare('SELECT id, name, data, created_at FROM plans WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?').all(req.user.id, limit, offset), total);
  });

  route('POST', /^\/api\/plans$/, true, async (req, res, m, body) => {
    const { name, data } = body;
    const nm = cleanText(name, 50);
    const json = data === undefined ? null : jsonWithin(data, 200000);
    if (!nm || !json) return fail(res, 400, '缺少计划内容或内容过大');
    const info = db.prepare('INSERT INTO plans (user_id, name, data) VALUES (?, ?, ?)')
      .run(req.user.id, nm, json);
    ok(res, { id: Number(info.lastInsertRowid) });
  });

  route('DELETE', /^\/api\/plans\/(\d+)$/, true, async (req, res, m) => {
    db.prepare('DELETE FROM plans WHERE id = ? AND user_id = ?').run(Number(m[1]), req.user.id);
    ok(res);
  });

  route('GET', /^\/api\/tracks$/, true, async (req, res) => {
    const kind = req.query.get('kind');
    const { limit, offset } = pageParams(req);
    let rows, total;
    if (kind) {
      total = db.prepare('SELECT COUNT(*) AS c FROM tracks WHERE user_id = ? AND kind = ?').get(req.user.id, String(kind)).c;
      rows = db.prepare('SELECT * FROM tracks WHERE user_id = ? AND kind = ? ORDER BY id DESC LIMIT ? OFFSET ?').all(req.user.id, String(kind), limit, offset);
    } else {
      total = db.prepare('SELECT COUNT(*) AS c FROM tracks WHERE user_id = ?').get(req.user.id).c;
      rows = db.prepare('SELECT * FROM tracks WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?').all(req.user.id, limit, offset);
    }
    okPage(res, rows, total);
  });

  route('POST', /^\/api\/tracks$/, true, async (req, res, m, body) => {
    const { kind, name, distance, duration, path: p, real } = body;
    const nm = cleanText(name, 50);
    const clean = cleanPath(p);
    if (!['run', 'ride'].includes(kind) || !nm || !validDistance(distance) || !validDuration(duration) || !clean) {
      return fail(res, 400, '记录数据不完整或不合法');
    }
    const pathJson = jsonWithin(clean, 500000);
    if (!pathJson) return fail(res, 400, '路径数据过大');
    const info = db.prepare('INSERT INTO tracks (user_id, kind, name, distance, duration, is_real, path) VALUES (?, ?, ?, ?, ?, ?, ?)')
      .run(req.user.id, kind, nm, distance, duration == null ? null : Number(duration), real ? 1 : 0, pathJson);
    ok(res, { id: Number(info.lastInsertRowid) });
  });

  route('DELETE', /^\/api\/tracks\/(\d+)$/, true, async (req, res, m) => {
    db.prepare('DELETE FROM tracks WHERE id = ? AND user_id = ?').run(Number(m[1]), req.user.id);
    ok(res);
  });

  route('GET', /^\/api\/checkins$/, true, async (req, res) => {
    const { limit, offset } = pageParams(req);
    const total = db.prepare('SELECT COUNT(*) AS c FROM checkins WHERE user_id = ?').get(req.user.id).c;
    okPage(res, db.prepare('SELECT * FROM checkins WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?').all(req.user.id, limit, offset), total);
  });

  route('POST', /^\/api\/checkins$/, true, async (req, res, m, body) => {
    const { name, note, emoji, lng, lat } = body;
    const nm = cleanText(name, 100);
    if (!nm || !validLngLat(lng, lat)) return fail(res, 400, '缺少有效打卡位置');
    const info = db.prepare('INSERT INTO checkins (user_id, name, note, emoji, lng, lat) VALUES (?, ?, ?, ?, ?, ?)')
      .run(req.user.id, nm, cleanText(note || '', 300), cleanText(emoji || '📍', 8), lng, lat);
    ok(res, { id: Number(info.lastInsertRowid) });
  });

  route('DELETE', /^\/api\/checkins\/(\d+)$/, true, async (req, res, m) => {
    db.prepare('DELETE FROM checkins WHERE id = ? AND user_id = ?').run(Number(m[1]), req.user.id);
    ok(res);
  });

  route('GET', /^\/api\/stats$/, true, async (req, res) => {
    const uid = req.user.id;
    const byKind = db.prepare(
      'SELECT kind, COUNT(*) AS count, SUM(distance) AS distance, SUM(COALESCE(duration,0)) AS duration, SUM(is_real) AS realCount FROM tracks WHERE user_id = ? GROUP BY kind'
    ).all(uid);
    const weekly = db.prepare(
      "SELECT strftime('%Y-%W', created_at) AS week, SUM(distance) AS distance, COUNT(*) AS count FROM tracks WHERE user_id = ? AND created_at >= date('now','localtime','-56 days') GROUP BY week ORDER BY week"
    ).all(uid);
    const counts = {
      favorites: db.prepare('SELECT COUNT(*) AS c FROM favorites WHERE user_id = ?').get(uid).c,
      plans: db.prepare('SELECT COUNT(*) AS c FROM plans WHERE user_id = ?').get(uid).c,
      checkins: db.prepare('SELECT COUNT(*) AS c FROM checkins WHERE user_id = ?').get(uid).c,
      tracks: db.prepare('SELECT COUNT(*) AS c FROM tracks WHERE user_id = ?').get(uid).c,
    };
    const recentCheckins = db.prepare('SELECT name, emoji, created_at FROM checkins WHERE user_id = ? ORDER BY id DESC LIMIT 5').all(uid);
    ok(res, { byKind, weekly, counts, recentCheckins, since: db.prepare('SELECT created_at FROM users WHERE id = ?').get(uid).created_at });
  });
};
