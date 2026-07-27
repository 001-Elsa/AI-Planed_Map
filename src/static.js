/* 静态文件服务:MIME 映射 + 路径穿越防护 */
'use strict';

const path = require('path');
const fs = require('fs');

const PUBLIC_DIR = path.resolve(__dirname, '..', 'public');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.webmanifest': 'application/manifest+json',
};

function serveStatic(req, res, pathname) {
  let rel;
  try { rel = decodeURIComponent(pathname); } catch (e) { res.writeHead(400); res.end('Bad Request'); return; }
  if (rel === '/') rel = '/index.html';
  const file = path.resolve(PUBLIC_DIR, '.' + rel);
  const inside = path.relative(PUBLIC_DIR, file);
  if (inside.startsWith('..') || path.isAbsolute(inside)) { res.writeHead(403); res.end('Forbidden'); return; }
  fs.readFile(file, (err, buf) => {
    if (err) {
      res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end('404 Not Found');
      return;
    }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file).toLowerCase()] || 'application/octet-stream' });
    res.end(buf);
  });
}

module.exports = { serveStatic, PUBLIC_DIR };
