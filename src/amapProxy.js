'use strict';

const https = require('https');
const { getSetting } = require('./db');
const { rateAllowed, rateHit } = require('./auth');

const AMAP_PROXY_RATE_MAX = parseInt(process.env.AMAP_PROXY_RATE_MAX, 10) || 300;
const AMAP_PROXY_WINDOW_MS = (parseInt(process.env.AMAP_PROXY_WINDOW_SEC, 10) || 60) * 1000;
const CACHE_TTL_MS = (parseInt(process.env.AMAP_PROXY_CACHE_SEC, 10) || 30) * 1000;
const CACHE_MAX_BYTES = 1024 * 1024;
const cache = new Map();

function plain(res, code, msg) {
  res.writeHead(code, { 'Content-Type': 'text/plain; charset=utf-8' });
  res.end(msg);
}

function cacheGet(key) {
  const hit = cache.get(key);
  if (!hit || hit.expiresAt < Date.now()) {
    cache.delete(key);
    return null;
  }
  return hit;
}

function cacheSet(key, value) {
  if (cache.size > 200) cache.delete(cache.keys().next().value);
  cache.set(key, Object.assign({ expiresAt: Date.now() + CACHE_TTL_MS }, value));
}

function amapProxy(req, res) {
  if (!['GET', 'POST', 'PUT'].includes(req.method)) return plain(res, 405, 'method not allowed');

  const rateKey = 'amap|' + (req.ip || req.socket.remoteAddress || '?');
  if (!rateAllowed(rateKey, AMAP_PROXY_RATE_MAX)) return plain(res, 429, 'too many amap proxy requests');
  rateHit(rateKey, AMAP_PROXY_WINDOW_MS);

  let u;
  try { u = new URL(req.url, 'http://localhost'); }
  catch (e) { return plain(res, 400, 'bad request'); }

  const restPath = u.pathname.slice('/_AMapService'.length) || '/';
  if (!/^\/v\d+\//.test(restPath)) return plain(res, 403, 'amap proxy path not allowed');

  const jscode = getSetting('amap_jscode');
  if (!jscode) return plain(res, 503, 'AMap jscode not configured');

  const rest = restPath + u.search;
  const cacheKey = req.method + ' ' + rest;
  if (req.method === 'GET') {
    const hit = cacheGet(cacheKey);
    if (hit) {
      res.writeHead(hit.status, hit.headers);
      res.end(hit.body);
      return;
    }
  }

  const sep = rest.includes('?') ? '&' : '?';
  const target = 'https://restapi.amap.com' + rest + sep + 'jscode=' + encodeURIComponent(jscode);
  const preq = https.request(target, { method: req.method, headers: { 'User-Agent': 'MapGo-Proxy' } }, (pres) => {
    const headers = Object.assign({}, pres.headers);
    delete headers['content-length'];
    const status = pres.statusCode || 502;

    if (req.method !== 'GET' || status >= 400) {
      res.writeHead(status, headers);
      pres.pipe(res);
      return;
    }

    const chunks = [];
    let size = 0;
    res.writeHead(status, headers);
    pres.on('data', (chunk) => {
      size += chunk.length;
      if (size <= CACHE_MAX_BYTES) chunks.push(chunk);
      res.write(chunk);
    });
    pres.on('end', () => {
      res.end();
      if (size <= CACHE_MAX_BYTES) cacheSet(cacheKey, { status, headers, body: Buffer.concat(chunks) });
    });
  });
  preq.on('error', () => {
    if (!res.headersSent) res.writeHead(502);
    res.end('proxy error');
  });
  if (req.method === 'POST' || req.method === 'PUT') req.pipe(preq);
  else preq.end();
}

module.exports = { amapProxy };
