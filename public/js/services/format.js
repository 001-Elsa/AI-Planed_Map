/* 纯函数工具:格式化、球面距离、坐标转换、GPX 导出 */
'use strict';

export function fmtDist(m) {
  return m >= 1000 ? (m / 1000).toFixed(m >= 10000 ? 0 : 1) + ' 公里' : Math.round(m) + ' 米';
}

export function fmtDur(s) {
  if (s >= 3600) return Math.floor(s / 3600) + ' 小时 ' + Math.round((s % 3600) / 60) + ' 分';
  return Math.max(1, Math.round(s / 60)) + ' 分钟';
}

export function fmtClock(d) {
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}

export function fmtMMSS(s) {
  return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(Math.floor(s % 60)).padStart(2, '0');
}

/* 球面距离(米),入参 {lng, lat} */
export function haversine(a, b) {
  const R = 6371000, rad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * rad, dLng = (b.lng - a.lng) * rad;
  const s = Math.sin(dLat / 2) ** 2 +
    Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(s));
}

/* AMap LngLat 或数组 → [lng, lat] */
export function toXY(pt) { return Array.isArray(pt) ? [pt[0], pt[1]] : [pt.lng, pt.lat]; }

/* GPX 导出(纯前端 Blob 下载) */
export function downloadGPX(name, pathArr) {
  const pts = (pathArr || []).map((p) => (Array.isArray(p) ? p : [p.lng, p.lat]));
  if (!pts.length) return false;
  const xml = '<?xml version="1.0" encoding="UTF-8"?>\n' +
    '<gpx version="1.1" creator="MapGo" xmlns="http://www.topografix.com/GPX/1/1">\n' +
    '<trk><name>' + String(name).replace(/[<>&]/g, '') + '</name><trkseg>\n' +
    pts.map((p) => '<trkpt lat="' + p[1] + '" lon="' + p[0] + '"></trkpt>').join('\n') +
    '\n</trkseg></trk></gpx>';
  const blob = new Blob([xml], { type: 'application/gpx+xml' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = String(name).replace(/[\\/:*?"<>|]/g, '_') + '.gpx';
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  return true;
}

/* 复制文本(带降级) */
export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; }
  catch (e) { try { prompt('复制下面的链接:', text); return true; } catch (e2) { return false; } }
}
