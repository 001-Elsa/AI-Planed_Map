/* 认证与安全:scrypt 密码、会话 token、好友关系、登录限流 */
'use strict';

const crypto = require('crypto');
const { db } = require('./db');

const TOKEN_DAYS = 30;

/* ---- 密码:scrypt 加盐,timingSafeEqual 防时序攻击 ---- */
function hashPassword(password) {
  const salt = crypto.randomBytes(16).toString('hex');
  const hash = crypto.scryptSync(String(password), salt, 64).toString('hex');
  return salt + ':' + hash;
}
function verifyPassword(password, stored) {
  const [salt, hash] = String(stored).split(':');
  if (!salt || !hash) return false;
  const calc = crypto.scryptSync(String(password), salt, 64);
  const orig = Buffer.from(hash, 'hex');
  return calc.length === orig.length && crypto.timingSafeEqual(calc, orig);
}

/* ---- 会话 ---- */
function issueToken(userId) {
  const token = crypto.randomBytes(32).toString('hex');
  db.prepare(
    "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, datetime('now','localtime', ?))"
  ).run(token, userId, '+' + TOKEN_DAYS + ' days');
  return token;
}

function userByToken(token) {
  if (!token) return null;
  const row = db.prepare(
    "SELECT s.user_id AS id, u.username, u.nickname, u.is_admin FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ? AND s.expires_at > datetime('now','localtime')"
  ).get(token);
  return row || null;
}

/* ---- 好友关系(任一方向 accepted) ---- */
function areFriends(a, b) {
  return !!db.prepare(
    "SELECT id FROM friends WHERE status = 'accepted' AND ((user_id = ? AND friend_id = ?) OR (user_id = ? AND friend_id = ?))"
  ).get(a, b, b, a);
}

/* ---- 登录/注册限流(内存滑动窗口,防暴力破解) ----
 * 登录:按 ip+username 记失败次数,成功即清零
 * 注册:按 ip 记所有尝试,防批量刷号
 * 窗口与阈值可用环境变量覆盖(测试用) */
const RL_MAX = parseInt(process.env.RATE_LIMIT_MAX, 10) || 10;
const RL_REG_MAX = parseInt(process.env.RATE_LIMIT_REG_MAX, 10) || 20;
const RL_WRITE_MAX = parseInt(process.env.RATE_LIMIT_WRITE_MAX, 10) || 120;  // 每分钟写操作上限/用户
const RL_WINDOW = (parseInt(process.env.RATE_LIMIT_WINDOW_SEC, 10) || 900) * 1000;
const buckets = new Map();

function bucketOf(key) {
  const b = buckets.get(key);
  if (!b || Date.now() > b.resetAt) return null;
  return b;
}
function rateAllowed(key, max) {
  const b = bucketOf(key);
  return !b || b.count < (max || RL_MAX);
}
function rateHit(key, windowMs) {
  const b = bucketOf(key);
  if (!b) buckets.set(key, { count: 1, resetAt: Date.now() + (windowMs || RL_WINDOW) });
  else b.count++;
}
function rateClear(key) { buckets.delete(key); }

/* 定期清空过期桶,防内存膨胀 */
const sweep = setInterval(() => {
  const now = Date.now();
  for (const [k, b] of buckets) if (now > b.resetAt) buckets.delete(k);
}, 60 * 1000);
sweep.unref();

module.exports = {
  hashPassword, verifyPassword, issueToken, userByToken, areFriends,
  rateAllowed, rateHit, rateClear, RL_MAX, RL_REG_MAX, RL_WRITE_MAX,
};
