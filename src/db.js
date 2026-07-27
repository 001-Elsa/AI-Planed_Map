/* ===================================================================
 * 数据库层:node:sqlite 连接、建表、迁移、settings 读写
 * 通过 DATA_DIR 环境变量可指定数据目录(测试时用临时目录)
 * =================================================================== */
'use strict';

const path = require('path');
const fs = require('fs');

let DatabaseSync;
try {
  ({ DatabaseSync } = require('node:sqlite'));
} catch (e) {
  console.error('本服务需要 Node.js >= 22.5(内置 SQLite 模块)。当前版本:' + process.version);
  console.error('请升级 Node:https://nodejs.org/');
  process.exit(1);
}

const dataDir = process.env.DATA_DIR
  ? path.resolve(process.env.DATA_DIR)
  : path.join(__dirname, '..', 'data');
if (!fs.existsSync(dataDir)) fs.mkdirSync(dataDir, { recursive: true });

const dbFile = path.join(dataDir, 'mapgo.db');
const db = new DatabaseSync(dbFile);
db.exec('PRAGMA journal_mode = WAL;');
db.exec('PRAGMA foreign_keys = ON;');

db.exec(`
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  nickname TEXT NOT NULL,
  pass_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS favorites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  address TEXT,
  lng REAL NOT NULL,
  lat REAL NOT NULL,
  mode TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  data TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS tracks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,                       -- run | ride
  name TEXT NOT NULL,
  distance REAL NOT NULL,                   -- 米
  duration REAL,                            -- 秒
  is_real INTEGER NOT NULL DEFAULT 0,       -- 1 = GPS 实录
  path TEXT NOT NULL,                       -- JSON [[lng,lat,t?],...]
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS checkins (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  note TEXT,
  emoji TEXT,
  lng REAL NOT NULL,
  lat REAL NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS shares (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token TEXT UNIQUE NOT NULL,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  type TEXT NOT NULL,                       -- track | plan
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS friends (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,   -- 发起人
  friend_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, -- 被加的人
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | accepted
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(user_id, friend_id)
);
CREATE INDEX IF NOT EXISTS idx_tracks_user ON tracks(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expire ON sessions(expires_at);
`);

/* 老库迁移(列已存在则忽略) */
try { db.exec('ALTER TABLE tracks ADD COLUMN is_real INTEGER NOT NULL DEFAULT 0'); } catch (e) { /* noop */ }
try { db.exec('ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0'); } catch (e) { /* noop */ }

/* settings k-v */
function getSetting(k) {
  const r = db.prepare('SELECT value FROM settings WHERE key = ?').get(k);
  return r ? r.value : null;
}
function setSetting(k, v) {
  db.prepare('INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value').run(k, v);
}

/* 环境变量注入服务端高德 Key(容器部署友好) */
if (process.env.AMAP_KEY) setSetting('amap_key', process.env.AMAP_KEY);
if (process.env.AMAP_JSCODE) setSetting('amap_jscode', process.env.AMAP_JSCODE);

/* 启动清理:过期会话 + 过期分享(默认 180 天) */
db.prepare("DELETE FROM sessions WHERE expires_at < datetime('now','localtime')").run();
const shareTtl = parseInt(process.env.SHARE_TTL_DAYS, 10) || 180;
db.prepare("DELETE FROM shares WHERE created_at < datetime('now','localtime', ?)").run('-' + shareTtl + ' days');

module.exports = { db, dbFile, getSetting, setSetting };
