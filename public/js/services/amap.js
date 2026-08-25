/* 高德地图服务:脚本加载、错误码解释、路线规划、POI 检索、城市识别 */
'use strict';

import { S } from '../state.js';
import { haversine } from './format.js';

let amapServiceKey = '';
let amapUsesProxy = false;

/* ---- 动态加载 JS API(两种安全模式) ----
 * useProxy=true:服务端托管 Key,serviceHost 走 /_AMapService 官方代理,jscode 不出服务器
 * useProxy=false:本机 jscode 模式 */
export function loadAMap(key, jscode, useProxy, onload, onerror) {
  amapServiceKey = String(key || '').trim();
  amapUsesProxy = Boolean(useProxy);
  window._AMapSecurityConfig = useProxy
    ? { serviceHost: location.origin + '/_AMapService' }
    : { securityJsCode: jscode };
  const s = document.createElement('script');
  s.src = 'https://webapi.amap.com/maps?v=2.0&key=' + encodeURIComponent(key) +
    '&plugin=AMap.AutoComplete,AMap.PlaceSearch,AMap.Walking,AMap.Riding,AMap.Driving,AMap.Transfer,AMap.Weather,AMap.HeatMap,AMap.Scale';
  s.onload = onload;
  s.onerror = onerror;
  document.head.appendChild(s);
}

/* ---- 高德错误码 → 用户能看懂的话 ----
 * 覆盖 Key 配置错误与配额类错误(10003 日调用量超限 / 10004·CUQPS 并发超限 等) */
const AMAP_ERRORS = [
  ['INVALID_USER_KEY', '地图 Key 无效,请联系管理员检查服务端配置'],
  ['INVALID_USER_SCODE', '地图安全密钥校验失败,请联系管理员检查服务端配置'],
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

async function fetchProxyJson(path, options, timeoutMs = 26000) {
  const params = new URLSearchParams({
    key: amapServiceKey,
    platform: 'JS',
    s: 'rsv3',
    logversion: '2.0',
    sdkversion: '2.3.5.6',
    appname: location.href.split('#')[0],
    language: 'zh_cn',
    csid: proxyRequestId(),
    ...options,
  });
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch('/_AMapService/' + path + '?' + params.toString(), {
      signal: controller.signal,
      headers: { Accept: 'application/json' },
    });
    if (!response.ok) throw new Error('地图服务请求失败(' + response.status + ')');
    return await response.json();
  } catch (error) {
    if (error && error.name === 'AbortError') throw new Error('地图服务响应超时，请重试');
    throw error;
  } finally {
    clearTimeout(timeout);
  }
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
function fallbackRouteLeg(from, to, err) {
  return {
    distance: haversine(from, to), time: null, instr: [],
    path: [[from.lng, from.lat], [to.lng, to.lat]], ok: false, err,
  };
}

function parsePolyline(polyline) {
  return String(polyline || '').split(';').map((point) => point.split(',').map(Number))
    .filter((point) => point.length === 2 && point.every(Number.isFinite));
}

async function proxyRouteLeg(from, to, tmode) {
  const path = tmode === 'drive'
    ? 'v3/direction/driving'
    : tmode === 'ride' ? 'v4/direction/bicycling' : 'v3/direction/walking';
  try {
    const result = await fetchProxyJson(path, {
      origin: from.lng + ',' + from.lat,
      destination: to.lng + ',' + to.lat,
      extensions: 'base',
    });
    const route = tmode === 'ride' ? result.data : result.route;
    const first = route && route.paths && route.paths[0];
    const success = tmode === 'ride'
      ? Number(result.errcode) === 0
      : String(result.status) === '1';
    if (!success || !first) {
      throw new Error(explainAmapError(result) || result.errmsg || result.info || '地图路线规划失败');
    }
    const steps = Array.isArray(first.steps) ? first.steps : [];
    const routePath = steps.flatMap((step) => parsePolyline(step.polyline));
    return {
      distance: Number(first.distance) || haversine(from, to),
      time: Number(first.duration) || null,
      instr: steps.map((step) => step.instruction).filter(Boolean),
      path: routePath.length ? routePath : [[from.lng, from.lat], [to.lng, to.lat]],
      ok: true,
    };
  } catch (error) {
    return fallbackRouteLeg(from, to, error && error.message ? error.message : '地图路线规划失败');
  }
}

async function transitRouteLeg(from, to, city) {
  try {
    const plans = await searchTransitPlans(from, to, city);
    const plan = plans[0];
    if (!plan) throw new Error('没有找到公共交通换乘方案');
    const segments = Array.isArray(plan.segments) ? plan.segments : [];
    const path = segments.flatMap((segment) =>
      segment.path || (segment.transit && segment.transit.path) || []);
    const instr = segments.map((segment) => segment.instruction).filter(Boolean);
    return {
      distance: Number(plan.distance) || haversine(from, to),
      time: Number(plan.time) || null,
      path: path.length ? path : [[from.lng, from.lat], [to.lng, to.lat]],
      instr,
      ok: true,
      source: 'amap_transit',
    };
  } catch (error) {
    return fallbackRouteLeg(
      from,
      to,
      error && error.message ? error.message : '公共交通换乘规划失败',
    );
  }
}

export function routeLeg(from, to, tmode, city = '') {
  if (tmode === 'transit') return transitRouteLeg(from, to, city);
  if (amapUsesProxy && amapServiceKey) return proxyRouteLeg(from, to, tmode);
  return new Promise((resolve) => {
    let settled = false;
    const fallback = (err) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      resolve(fallbackRouteLeg(from, to, err));
    };
    const timeout = setTimeout(() => fallback('地图路线绘制超时，已使用直线示意'), 8000);
    const fin = (status, result) => {
      if (settled) return;
      if (status === 'complete' && result && result.routes && result.routes.length) {
        settled = true;
        clearTimeout(timeout);
        const r = result.routes[0];
        resolve({ distance: r.distance, time: r.time, path: flattenRoutePath(r), instr: routeInstructions(r), ok: true });
      } else {
        fallback(explainAmapError(result));
      }
    };
    try {
      let planner;
      if (tmode === 'drive') planner = new AMap.Driving({ policy: 0 });
      else if (tmode === 'ride') planner = new AMap.Riding({ policy: 0 });
      else planner = new AMap.Walking({});
      planner.search([from.lng, from.lat], [to.lng, to.lat], fin);
    } catch (error) {
      fallback(error && error.message ? error.message : '地图路线绘制不可用，已使用直线示意');
    }
  });
}

function normalizeTransitPlan(plan) {
  const segments = [];
  (plan.segments || []).forEach((segment) => {
    const walking = segment.walking || {};
    const walkSteps = Array.isArray(walking.steps) ? walking.steps : [];
    const walkPath = walkSteps.flatMap((step) => parsePolyline(step.polyline));
    if (walkPath.length) {
      segments.push({
        transit_mode: 'WALK',
        path: walkPath,
        instruction: walkSteps.map((step) => step.instruction).filter(Boolean).join('；') || '步行',
      });
    }
    const buslines = segment.bus && Array.isArray(segment.bus.buslines) ? segment.bus.buslines : [];
    buslines.forEach((line) => {
      const name = String(line.name || '公交');
      const subway = /地铁|轻轨|轨道/.test(name + ' ' + String(line.type || ''));
      const linePath = parsePolyline(line.polyline);
      segments.push({
        transit_mode: subway ? 'SUBWAY' : 'BUS',
        path: linePath,
        transit: { lines: [{ name }], path: linePath },
        instruction: name,
      });
    });
  });
  return {
    distance: Number(plan.distance) || 0,
    time: Number(plan.duration) || 0,
    cost: Number(plan.cost) || 0,
    nightLine: String(plan.nightflag || '') === '1',
    segments,
  };
}

export async function searchTransitPlans(from, to, city) {
  if (amapUsesProxy && amapServiceKey) {
    const result = await fetchProxyJson('v3/direction/transit/integrated', {
      origin: from.lng + ',' + from.lat,
      destination: to.lng + ',' + to.lat,
      city: city || '全国',
      cityd: city || '',
      strategy: '0',
      extensions: 'base',
    }, 28000);
    if (String(result.status) !== '1' || !result.route) {
      throw new Error(explainAmapError(result) || result.info || '公交换乘规划失败');
    }
    return (result.route.transits || []).slice(0, 3).map(normalizeTransitPlan);
  }
  return new Promise((resolve, reject) => {
    const transfer = new AMap.Transfer({ city: city || '全国', policy: 0 });
    transfer.search([from.lng, from.lat], [to.lng, to.lat], (status, result) => {
      if (status === 'complete' && result && result.plans) resolve(result.plans.slice(0, 3));
      else reject(new Error(explainAmapError(result) || '没有找到换乘方案'));
    });
  });
}

export async function getLiveWeather(city) {
  if (amapUsesProxy && amapServiceKey) {
    const result = await fetchProxyJson('v3/weather/weatherInfo', {
      city,
      extensions: 'base',
    });
    if (String(result.status) !== '1' || !result.lives || !result.lives[0]) {
      throw new Error(explainAmapError(result) || result.info || '天气加载失败');
    }
    const live = result.lives[0];
    return {
      city: live.city,
      weather: live.weather,
      temperature: live.temperature,
      windDirection: live.winddirection,
      windPower: live.windpower,
      humidity: live.humidity,
    };
  }
  return new Promise((resolve, reject) => {
    const weather = new AMap.Weather();
    weather.getLive(city, (error, data) => error || !data ? reject(error || new Error('天气加载失败')) : resolve(data));
  });
}

/* ---- POI:就近取一个(计划模式/打卡命名用) ---- */
export function searchNearestPOI(keyword, origin) {
  return searchNearbyPlaces({
    keyword,
    center: origin,
    radius: 8000,
    pageSize: 5,
    pages: 1,
    extensions: 'base',
  }).then((pois) => {
    pois.sort((a, b) => haversine(origin, a.location) - haversine(origin, b.location));
    return pois[0] || null;
  }).catch(() => null);
}

/* ---- POI:全城关键词取第一个(找中间点用) ---- */
export async function searchPlaceByKeyword(kw) {
  if (amapUsesProxy && amapServiceKey) {
    try {
      const result = await fetchProxyJson('v3/place/text', {
        keywords: String(kw || '').trim(),
        city: '',
        citylimit: 'false',
        offset: '1',
        page: '1',
        extensions: 'base',
      });
      const poi = result.pois && result.pois[0];
      return nearbyLocation(poi);
    } catch (error) {
      return null;
    }
  }
  return new Promise((resolve) => {
    const ps = new AMap.PlaceSearch({ pageSize: 1, pageIndex: 1, extensions: 'base' });
    ps.search(kw, (status, result) => {
      const p = status === 'complete' && result.poiList && result.poiList.pois[0];
      resolve(p && p.location ? p : null);
    });
  });
}

function normalizePlaceSuggestion(p) {
  if (!p) return null;
  let lng;
  let lat;
  if (typeof p.location === 'string') {
    [lng, lat] = p.location.split(',').map(Number);
  } else if (p.location) {
    lng = Number(p.location.lng);
    lat = Number(p.location.lat);
  }
  if (!Number.isFinite(lng) || !Number.isFinite(lat)) return null;
  return { ...p, location: { lng, lat } };
}

async function searchProxyPlaceSuggestions(kw, limit, onPartial) {
  const requestId = () => globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function'
    ? globalThis.crypto.randomUUID().replace(/-/g, '')
    : Date.now().toString(36) + Math.random().toString(36).slice(2);
  const common = {
    key: amapServiceKey,
    platform: 'JS',
    s: 'rsv3',
    logversion: '2.0',
    sdkversion: '2.3.5.6',
    appname: encodeURIComponent(location.href.split('#')[0]),
    language: 'zh_cn',
  };

  const fetchAmap = async (path, options) => {
    const params = new URLSearchParams({ ...common, ...options, csid: requestId() });
    const url = '/_AMapService/' + path + '?' + params.toString();
    let response;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        response = await fetch(url);
        if (response.ok || response.status < 500) break;
      } catch (error) {
        if (attempt === 1) throw error;
      }
      await new Promise((resolve) => setTimeout(resolve, 150));
    }
    if (!response || !response.ok) throw new Error('地图地点搜索请求失败');
    const result = await response.json();
    if (String(result.status) !== '1') throw new Error(result.info || '地图地点搜索失败');
    return result;
  };

  const score = (p) => {
    const query = kw.toLowerCase();
    const name = String(p.name || '').toLowerCase();
    const administrativeName = name.replace(/[省市区县]$/, '');
    if (String(p.id || '').startsWith('district-') && administrativeName === query) return -1;
    if (name === query) return 0;
    if (name.startsWith(query)) return 1;
    if (name.includes(query)) return 2;
    return 3;
  };
  const merge = (...groups) => {
    const seen = new Set();
    return groups.flat().filter(Boolean).filter((p) => {
      const key = p.id || [p.name, p.location.lng, p.location.lat].join('|');
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    }).map((p, index) => ({ p, index })).sort((a, b) => score(a.p) - score(b.p) || a.index - b.index)
      .map((item) => item.p).slice(0, limit);
  };

  let combined = [];
  const publish = (items) => {
    combined = merge(combined, items);
    if (typeof onPartial === 'function' && combined.length) onPartial(combined);
  };
  const poiRequest = fetchAmap('v3/assistant/inputtips', {
    keywords: kw, city: '', type: '', citylimit: 'false', datatype: 'poi',
  }).then((result) => (Array.isArray(result.tips) ? result.tips : [])
    .map(normalizePlaceSuggestion).filter(Boolean)).then((items) => { publish(items); return items; });
  const districtRequest = fetchAmap('v3/config/district', {
    keywords: kw, subdistrict: '0', extensions: 'base',
  }).then((result) => (Array.isArray(result.districts) ? result.districts : []).map((item) =>
    normalizePlaceSuggestion({
      id: 'district-' + item.adcode,
      name: item.name,
      district: ({ province: '省级行政区', city: '城市', district: '区县' })[item.level] || '行政区',
      address: '行政区中心',
      adcode: item.adcode,
      location: item.center,
    }))).then((items) => { publish(items.filter(Boolean)); return items; });

  const settled = await Promise.allSettled([poiRequest, districtRequest]);
  if (combined.length) return combined;
  const failure = settled.find((item) => item.status === 'rejected');
  throw failure && failure.reason || new Error('没有找到匹配地点');
}

/* ---- 全国地点候选:模糊匹配全部 POI 类型，不要求用户先输入行政区 ---- */
export function searchPlaceSuggestions(kw, limit = 20, onPartial) {
  if (amapUsesProxy && amapServiceKey) return searchProxyPlaceSuggestions(kw, limit, onPartial);
  return new Promise((resolve, reject) => {
    const autocomplete = new AMap.AutoComplete({
      citylimit: false,
      datatype: 'poi',
    });
    autocomplete.search(kw, (status, result) => {
      if (status !== 'complete') {
        reject(new Error('地图地点搜索失败'));
        return;
      }
      // 新版 SDK 直接返回 Tip[]，旧版返回 { tips: Tip[] }。
      const tips = Array.isArray(result) ? result : result && result.tips;
      resolve((Array.isArray(tips) ? tips : [])
        .map(normalizePlaceSuggestion)
        .filter(Boolean)
        .slice(0, limit));
    });
  });
}

function proxyRequestId() {
  return globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function'
    ? globalThis.crypto.randomUUID().replace(/-/g, '')
    : Date.now().toString(36) + Math.random().toString(36).slice(2);
}

function nearbyLocation(p) {
  if (!p) return null;
  const normalized = normalizePlaceSuggestion(p);
  if (!normalized) return null;
  const distance = Number(p.distance);
  return {
    ...normalized,
    distance: Number.isFinite(distance) ? distance : undefined,
  };
}

async function fetchProxyNearby(options, page) {
  const params = new URLSearchParams({
    key: amapServiceKey,
    platform: 'JS',
    s: 'rsv3',
    logversion: '2.0',
    sdkversion: '2.3.5.6',
    appname: location.href.split('#')[0],
    language: 'zh_cn',
    csid: proxyRequestId(),
    keywords: options.keyword,
    types: options.type,
    location: options.location,
    radius: String(options.radius),
    sortrule: 'distance',
    offset: String(options.pageSize),
    page: String(page),
    extensions: options.extensions,
  });
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 26000);
  try {
    const response = await fetch('/_AMapService/v3/place/around?' + params.toString(), {
      signal: controller.signal,
      headers: { Accept: 'application/json' },
    });
    if (!response.ok) throw new Error('地图周边搜索请求失败(' + response.status + ')');
    const result = await response.json();
    if (String(result.status) !== '1') {
      const error = new Error(result.info || '地图周边搜索失败');
      error.amapResult = result;
      throw error;
    }
    return Array.isArray(result.pois) ? result.pois.map(nearbyLocation).filter(Boolean) : [];
  } catch (error) {
    if (error && error.name === 'AbortError') throw new Error('地图周边搜索超时，请重试');
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

function searchNearbyWithJsApi(options) {
  return new Promise((resolve, reject) => {
    const ps = new AMap.PlaceSearch({
      type: options.type,
      pageSize: options.pageSize,
      pageIndex: 1,
      extensions: options.extensions,
    });
    ps.searchNearBy(options.keyword, options.center, options.radius, (status, result) => {
      if (status === 'complete' && result && result.poiList) {
        resolve((result.poiList.pois || []).filter((p) => p.location));
        return;
      }
      const error = new Error(explainAmapError(result) || '地图周边搜索失败');
      error.amapResult = result;
      reject(error);
    });
  });
}

/* ---- POI:模式周边批量搜索
 * 代理模式直接请求高德 Web 服务，避免 PlaceSearch 插件在 serviceHost 模式下
 * 偶发把业务错误折叠成空列表；本机 Key 模式继续使用 JS API。 */
export async function searchNearbyPlaces({
  keyword,
  type = '',
  center,
  radius = 5000,
  pageSize = 25,
  pages = 1,
  extensions = 'base',
}) {
  const lng = Number(center && center.lng !== undefined ? center.lng : center && center[0]);
  const lat = Number(center && center.lat !== undefined ? center.lat : center && center[1]);
  if (!Number.isFinite(lng) || !Number.isFinite(lat)) throw new Error('地图中心点无效，请重新选择地点');
  const safeOptions = {
    keyword: String(keyword || '').trim(),
    type: String(type || '').trim(),
    center: [lng, lat],
    location: lng + ',' + lat,
    radius: Math.max(500, Math.min(50000, Math.round(Number(radius) || 5000))),
    pageSize: Math.max(1, Math.min(25, Math.round(Number(pageSize) || 25))),
    extensions: extensions === 'all' ? 'all' : 'base',
  };
  const pageCount = Math.max(1, Math.min(3, Math.round(Number(pages) || 1)));
  if (!amapUsesProxy || !amapServiceKey) return searchNearbyWithJsApi(safeOptions);

  const settled = await Promise.allSettled(
    Array.from({ length: pageCount }, (_, index) => fetchProxyNearby(safeOptions, index + 1))
  );
  const successful = settled.filter((item) => item.status === 'fulfilled');
  if (!successful.length) throw settled[0].reason;
  let pois = successful.flatMap((item) => item.value);

  // 某些细分类在部分城市没有类型数据，保留关键词回退，避免错误地显示“附近没有”。
  if (!pois.length && safeOptions.type) {
    pois = await fetchProxyNearby({ ...safeOptions, type: '' }, 1);
  }
  const seen = new Set();
  return pois.filter((p) => {
    const key = p.id || [p.name, p.location.lng, p.location.lat].join('|');
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/* ---- GPS(WGS-84) → 高德(GCJ-02)，国内否则会偏几十到几百米 ---- */
export function convertGpsToAmap(lng, lat) {
  return new Promise((resolve) => {
    const fallback = { lng: Number(lng), lat: Number(lat) };
    if (!window.AMap || typeof AMap.convertFrom !== 'function') {
      resolve(fallback);
      return;
    }
    AMap.convertFrom([fallback.lng, fallback.lat], 'gps', (status, result) => {
      const point = result && result.locations && result.locations[0];
      if (status === 'complete' && point) {
        resolve({ lng: Number(point.lng), lat: Number(point.lat) });
        return;
      }
      resolve(fallback);
    });
  });
}

function formatRegeoName(regeo) {
  if (!regeo) return '';
  const formatted = String(regeo.formattedAddress || regeo.formatted_address || '').trim();
  if (formatted) return formatted;
  const component = regeo.addressComponent || {};
  const parts = [
    component.province,
    component.city,
    component.district,
    component.township,
    component.street,
    component.streetNumber || component.street_number,
  ].map((part) => (Array.isArray(part) ? part[0] : part)).map((part) => String(part || '').trim()).filter(Boolean);
  return parts.filter((part, index) => index === 0 || part !== parts[index - 1]).join('');
}

export async function reverseGeocode(lng, lat) {
  if (!Number.isFinite(Number(lng)) || !Number.isFinite(Number(lat))) return '';
  if (amapUsesProxy && amapServiceKey) {
    const result = await fetchProxyJson('v3/geocode/regeo', {
      location: Number(lng) + ',' + Number(lat),
      radius: '100',
      extensions: 'base',
    });
    if (String(result.status) !== '1') return '';
    return formatRegeoName(result.regeocode);
  }
  return new Promise((resolve) => {
    if (!window.AMap) { resolve(''); return; }
    const finish = (status, result) => {
      resolve(status === 'complete' ? formatRegeoName(result && result.regeocode) : '');
    };
    try {
      AMap.plugin('AMap.Geocoder', () => {
        const geocoder = new AMap.Geocoder({ radius: 100, extensions: 'base' });
        geocoder.getAddress([Number(lng), Number(lat)], finish);
      });
    } catch (error) {
      resolve('');
    }
  });
}

/* ---- 当前城市(公交换乘/天气用) ---- */
export async function getCity() {
  if (amapUsesProxy && amapServiceKey && S.map) {
    try {
      const center = S.map.getCenter();
      const result = await fetchProxyJson('v3/geocode/regeo', {
        location: center.lng + ',' + center.lat,
        radius: '1000',
        extensions: 'base',
      });
      const component = result.regeocode && result.regeocode.addressComponent || {};
      const city = Array.isArray(component.city) ? component.city[0] : component.city;
      return String(city || component.province || '').trim();
    } catch (error) { /* 回退到地图实例识别 */ }
  }
  return new Promise((resolve) => {
    try { S.map.getCity((info) => resolve(info && (info.city || info.province) || '')); }
    catch (e) { resolve(''); }
  });
}
