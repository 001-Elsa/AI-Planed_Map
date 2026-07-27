/* 高德地图服务:脚本加载、错误码解释、路线规划、POI 检索、城市识别 */
'use strict';

import { S } from '../state.js';
import { haversine } from './format.js';

/* ---- 动态加载 JS API(两种安全模式) ----
 * useProxy=true:服务端托管 Key,serviceHost 走 /_AMapService 官方代理,jscode 不出服务器
 * useProxy=false:本机 jscode 模式 */
export function loadAMap(key, jscode, useProxy, onload, onerror) {
  window._AMapSecurityConfig = useProxy
    ? { serviceHost: location.origin + '/_AMapService' }
    : { securityJsCode: jscode };
  const s = document.createElement('script');
  s.src = 'https://webapi.amap.com/maps?v=2.0&key=' + encodeURIComponent(key) +
    '&plugin=AMap.PlaceSearch,AMap.Walking,AMap.Riding,AMap.Driving,AMap.Transfer,AMap.Weather,AMap.HeatMap,AMap.Geolocation,AMap.Scale';
  s.onload = onload;
  s.onerror = onerror;
  document.head.appendChild(s);
}

/* ---- 高德错误码 → 用户能看懂的话 ----
 * 覆盖 Key 配置错误与配额类错误(10003 日调用量超限 / 10004·CUQPS 并发超限 等) */
const AMAP_ERRORS = [
  ['INVALID_USER_KEY', 'Key 无效,请点右上 ⚙ 检查'],
  ['INVALID_USER_SCODE', '安全密钥(jscode)校验失败,请点右上 ⚙ 重新填写'],
  ['USERKEY_PLAT_NOMATCH', 'Key 平台类型不对:需在高德控制台选「Web端(JS API)」'],
  ['DAILY_QUERY_OVER_LIMIT', '该 Key 今日调用量已用完(10003),明天恢复或到高德控制台提额'],
  ['USER_DAILY_QUERY_OVER_LIMIT', '该 Key 今日调用量已用完,明天恢复或提额'],
  ['CUQPS_HAS_EXCEEDED_THE_LIMIT', '请求太频繁(并发超限),歇几秒再操作'],
  ['CQPS_HAS_EXCEEDED_THE_LIMIT', '请求太频繁(并发超限),歇几秒再操作'],
  ['USER_VISIT_TOO_FREQUENTLY', '操作太快啦,稍等几秒'],
  ['INSUFFICIENT_PRIVILEGES', 'Key 没有该服务的权限,检查高德控制台的服务开通情况'],
  ['10003', '该 Key 今日调用量已用完(10003)'],
  ['10004', '请求太频繁(10004),歇几秒再试'],
  ['10007', '数字签名校验失败(10007),检查安全密钥配置'],
];

/* result 可能是字符串(如 'CUQPS_HAS_EXCEEDED_THE_LIMIT')或含 info/infocode/message 的对象 */
export function explainAmapError(result) {
  const s = String(
    typeof result === 'string' ? result :
      (result && (result.info || result.infocode || result.message || result.type)) || ''
  ).toUpperCase();
  if (!s) return null;
  for (const [code, msg] of AMAP_ERRORS) {
    if (s.includes(code)) return msg;
  }
  return null;
}

/* ---- 路线结果解析 ---- */
export function flattenRoutePath(r) {
  const path = [];
  (r.rides || r.steps || []).forEach((seg) => (seg.path || []).forEach((pt) => path.push(pt)));
  return path;
}
export function routeInstructions(r) {
  return (r.rides || r.steps || []).map((seg) => seg.instruction).filter(Boolean);
}

/* ---- 单段路线规划,失败回退直线 ----
 * 返回 {distance, time, path, instr, ok, err?} */
export function routeLeg(from, to, tmode) {
  return new Promise((resolve) => {
    const fin = (status, result) => {
      if (status === 'complete' && result.routes && result.routes.length) {
        const r = result.routes[0];
        resolve({ distance: r.distance, time: r.time, path: flattenRoutePath(r), instr: routeInstructions(r), ok: true });
      } else {
        resolve({
          distance: haversine(from, to), time: null, instr: [],
          path: [[from.lng, from.lat], [to.lng, to.lat]], ok: false,
          err: explainAmapError(result),
        });
      }
    };
    let planner;
    if (tmode === 'drive') planner = new AMap.Driving({ policy: 0 });
    else if (tmode === 'ride') planner = new AMap.Riding({ policy: 0 });
    else planner = new AMap.Walking({});
    planner.search([from.lng, from.lat], [to.lng, to.lat], fin);
  });
}

/* ---- POI:就近取一个(计划模式/打卡命名用) ---- */
export function searchNearestPOI(keyword, origin) {
  return new Promise((resolve) => {
    const ps = new AMap.PlaceSearch({ pageSize: 5, pageIndex: 1, extensions: 'base' });
    ps.searchNearBy(keyword, [origin.lng, origin.lat], 8000, (status, result) => {
      if (status === 'complete' && result.poiList && result.poiList.pois.length) {
        const pois = result.poiList.pois.filter((p) => p.location);
        pois.sort((a, b) => haversine(origin, a.location) - haversine(origin, b.location));
        resolve(pois[0] || null);
      } else resolve(null);
    });
  });
}

/* ---- POI:全城关键词取第一个(找中间点用) ---- */
export function searchPlaceByKeyword(kw) {
  return new Promise((resolve) => {
    const ps = new AMap.PlaceSearch({ pageSize: 1, pageIndex: 1, extensions: 'base' });
    ps.search(kw, (status, result) => {
      const p = status === 'complete' && result.poiList && result.poiList.pois[0];
      resolve(p && p.location ? p : null);
    });
  });
}

/* ---- 当前城市(公交换乘/天气用) ---- */
export function getCity() {
  return new Promise((resolve) => {
    try { S.map.getCity((info) => resolve(info && (info.city || info.province) || '')); }
    catch (e) { resolve(''); }
  });
}
