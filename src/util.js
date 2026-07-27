/* HTTP 工具:统一响应格式、请求体解析、日志 */
'use strict';

function sendJson(res, code, obj) {
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(obj));
}
const ok = (res, data) => sendJson(res, 200, { ok: true, data: data === undefined ? null : data });
const fail = (res, code, msg) => sendJson(res, code, { ok: false, msg });

const MAX_BODY = 2 * 1024 * 1024;

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (c) => {
      size += c.length;
      if (size > MAX_BODY) { reject(new Error('请求体过大')); req.destroy(); return; }
      chunks.push(c);
    });
    req.on('end', () => {
      if (!chunks.length) return resolve({});
      try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
      catch (e) { reject(new Error('JSON 解析失败')); }
    });
    req.on('error', reject);
  });
}

/* 分页参数:?limit=&offset=(默认 100,上限 500) */
function pageParams(req) {
  const limit = Math.min(500, Math.max(1, parseInt(req.query.get('limit'), 10) || 100));
  const offset = Math.max(0, parseInt(req.query.get('offset'), 10) || 0);
  return { limit, offset };
}

/* 分页响应:数组仍是 data(向后兼容),总数放 X-Total-Count 头 */
function okPage(res, rows, total) {
  res.setHeader('X-Total-Count', String(total));
  ok(res, rows);
}

/* 访问日志:METHOD path status 耗时 */
function accessLog(req, res, startMs) {
  const line = `[${new Date().toISOString()}] ${req.method} ${req.url.split('?')[0]} ${res.statusCode} ${Date.now() - startMs}ms`;
  console.log(line);
}

module.exports = { sendJson, ok, fail, readBody, accessLog, pageParams, okPage };
