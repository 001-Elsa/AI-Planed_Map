/* POI 聚焦模式(吃货/厕所/逛街/停车/加油/救急/酒店):
 * 视野搜索、分类 chips、排序、详情窗、到这去、最近的一个、找中间点 */
'use strict';

import { S } from '../state.js';
import { $, escapeHtml, toast } from '../ui/dom.js';
import { API } from '../services/api.js?v=33';
import { fmtDist, fmtDur, haversine } from '../services/format.js';
import {
  explainAmapError,
  searchNearbyPlaces,
  searchPlaceByKeyword,
  flattenRoutePath,
} from '../services/amap.js?v=33';
import { geometricMedian } from '../services/algo.js';
import { requireLogin } from '../ui/auth.js?v=33';
import { MODES } from './registry.js?v=33';

let poiSearchSeq = 0;

/* ---------------- 生命周期 ---------------- */
export function activate(cfg) {
  renderChips(cfg);
  $('btn-nearest').classList.toggle('hidden', !cfg.nearest);
  if (cfg.nearest) $('btn-nearest').textContent = cfg.nearest;
  $('poi-count').textContent = '正在搜索…';
  $('poi-list').innerHTML = '';
  searchPOIsInView();
}

export function clearAll() {
  poiSearchSeq += 1;
  clearPoiMarkers();
  clearQuickOverlays();
  clearMeetOverlays();
}

export function bindPoiUI() {
  $('btn-poi-search').addEventListener('click', () => searchPOIsInView());
  $('poi-search').addEventListener('keydown', (e) => { if (e.key === 'Enter') searchPOIsInView(); });
  document.querySelectorAll('#poi-sort-seg .seg-btn').forEach((b) => {
    b.addEventListener('click', () => {
      S.sortBy = b.dataset.sort;
      document.querySelectorAll('#poi-sort-seg .seg-btn').forEach((x) => x.classList.toggle('active', x === b));
      renderPoiResults();
    });
  });
  $('btn-nearest').addEventListener('click', gotoNearest);

  /* 找中间点 */
  $('btn-meet').addEventListener('click', () => { $('meet-mask').classList.remove('hidden'); });
  $('btn-meet-cancel').addEventListener('click', () => $('meet-mask').classList.add('hidden'));
  $('btn-meet-go').addEventListener('click', runMeet);
}

/* ---------------- 搜索与渲染 ---------------- */
function renderChips(cfg) {
  const box = $('poi-chips');
  box.innerHTML = '';
  (cfg.chips || []).forEach((c, i) => {
    const b = document.createElement('button');
    b.className = 'chip' + (i === S.activeChipIdx ? ' active' : '');
    b.textContent = c.label;
    b.addEventListener('click', () => {
      S.activeChipIdx = i;
      $('poi-search').value = '';
      box.querySelectorAll('.chip').forEach((x, j) => x.classList.toggle('active', j === i));
      searchPOIsInView();
    });
    box.appendChild(b);
  });
}

function clearPoiMarkers() {
  if (S.poiMarkers.length) S.map.remove(S.poiMarkers);
  S.poiMarkers = [];
  S.lastPois = [];
}

function currentSearchParams(cfg) {
  const chip = (cfg.chips || [])[S.activeChipIdx] || {};
  const custom = $('poi-search').value.trim();
  const kw = custom || chip.kw || cfg.kw0 || '';
  const type = custom ? '' : (chip.type !== undefined ? chip.type : cfg.type) || '';
  return { kw, type };
}

export async function searchPOIsInView() {
  const cfg = MODES[S.currentMode];
  if (!cfg || !cfg.poi || !S.map) return;

  const { kw, type } = currentSearchParams(cfg);
  const center = S.map.getCenter();
  const zoom = S.map.getZoom();
  const radius = Math.min(10000, Math.max(2000, Math.round(50000 / Math.pow(1.7, zoom - 10))));
  const modeAtCall = S.currentMode;
  const requestSeq = ++poiSearchSeq;
  $('poi-count').textContent = '正在搜索…';
  try {
    const pois = await searchNearbyPlaces({
      keyword: kw,
      type,
      center,
      radius,
      pageSize: 25,
      // 首屏单请求最多 25 个标记，避免两页并发触发高德限流或慢请求超时。
      pages: 1,
      extensions: cfg.ext || 'base',
    });
    if (S.currentMode !== modeAtCall || requestSeq !== poiSearchSeq) return;
    clearPoiMarkers();
    if (!pois.length) {
      $('poi-count').textContent = '这个范围内暂时没有匹配地点，请移动地图或换个分类';
      $('poi-list').innerHTML = '';
      return;
    }
    S.lastPois = pois;
    renderPoiResults();
  } catch (error) {
    if (S.currentMode !== modeAtCall || requestSeq !== poiSearchSeq) return;
    clearPoiMarkers();
    const why = explainAmapError(error && (error.amapResult || error)) || error.message || '地图周边搜索失败';
    $('poi-count').textContent = why;
    $('poi-list').innerHTML = '';
    toast(why, 4000);
  }
}

function poiRating(p) {
  const r = p.biz_ext && p.biz_ext.rating;
  const n = parseFloat(Array.isArray(r) ? r[0] : r);
  return isNaN(n) ? null : n;
}
function poiCost(p) {
  const c = p.biz_ext && p.biz_ext.cost;
  const n = parseFloat(Array.isArray(c) ? c[0] : c);
  return isNaN(n) ? null : n;
}

function renderPoiResults() {
  const cfg = MODES[S.currentMode];
  if (!cfg || !cfg.poi) return;
  clearQuickOverlays();
  if (S.poiMarkers.length) { S.map.remove(S.poiMarkers); S.poiMarkers = []; }

  const origin = S.myPos || (() => { const c = S.map.getCenter(); return { lng: c.lng, lat: c.lat }; })();
  const pois = S.lastPois.slice();
  if (S.sortBy === 'rating' && cfg.showRating) {
    pois.sort((a, b) => (poiRating(b) || 0) - (poiRating(a) || 0));
  } else {
    pois.sort((a, b) => haversine(origin, a.location) - haversine(origin, b.location));
  }

  $('poi-count').textContent = '找到 ' + pois.length + ' 个地点';
  const list = $('poi-list');
  list.innerHTML = '';

  pois.forEach((p) => {
    const marker = new AMap.Marker({
      position: [p.location.lng, p.location.lat],
      content: '<div class="poi-marker" style="--mk:' + cfg.color + '">' + cfg.emoji + '</div>',
      offset: new AMap.Pixel(-17, -30),
      zIndex: 120,
    });
    marker.on('click', () => openPoiInfo(p, cfg));
    S.poiMarkers.push(marker);

    const d = haversine(origin, p.location);
    const rating = cfg.showRating ? poiRating(p) : null;
    const cost = cfg.showRating ? poiCost(p) : null;
    const meta = [];
    if (rating != null) meta.push('⭐' + rating.toFixed(1));
    if (cost != null) meta.push('¥' + Math.round(cost) + '/人');

    const item = document.createElement('div');
    item.className = 'poi-item';
    item.innerHTML =
      '<div><div class="name">' + escapeHtml(p.name) +
      (meta.length ? ' <span class="meta">' + meta.join(' · ') + '</span>' : '') + '</div>' +
      '<div class="addr">' + escapeHtml(p.address || p.type || '') + '</div></div>' +
      '<div class="dist">' + fmtDist(d) + '</div>';
    item.addEventListener('click', () => {
      S.map.setZoomAndCenter(17, [p.location.lng, p.location.lat]);
      openPoiInfo(p, cfg);
    });
    list.appendChild(item);
  });
  S.map.add(S.poiMarkers);
}

/* ---------------- 详情窗(收藏 / 到这去 / 高德导航) ---------------- */
export function openPoiInfo(p, cfg) {
  const origin = S.myPos;
  const d = origin ? haversine(origin, p.location) : null;
  const rating = poiRating(p), cost = poiCost(p);

  const box = document.createElement('div');
  box.className = 'iw';
  box.innerHTML =
    '<b class="iw-title">' + cfg.emoji + ' ' + escapeHtml(p.name) + '</b>' +
    (rating != null || cost != null
      ? '<div class="iw-meta">' + (rating != null ? '⭐ ' + rating.toFixed(1) + ' ' : '') +
        (cost != null ? '¥' + Math.round(cost) + '/人' : '') + '</div>' : '') +
    (p.address ? '<div class="iw-addr">' + escapeHtml(p.address) + '</div>' : '') +
    (p.tel ? '<div class="iw-addr">☎ ' + escapeHtml(String(p.tel)) + '</div>' : '') +
    (d != null ? '<div class="iw-addr">距离我约 <b>' + fmtDist(d) + '</b></div>' : '');

  const btns = document.createElement('div');
  btns.className = 'iw-btns';

  const favBtn = document.createElement('button');
  favBtn.textContent = '⭐ 收藏';
  favBtn.addEventListener('click', async () => {
    if (!requireLogin()) return;
    try {
      await API.addFavorite({
        name: p.name, address: p.address || '',
        lng: p.location.lng, lat: p.location.lat, mode: S.currentMode,
      });
      toast('已收藏 ⭐');
    } catch (e) { toast(e.message); }
  });
  btns.appendChild(favBtn);

  const goBtn = document.createElement('button');
  goBtn.textContent = '🚶 到这去';
  goBtn.addEventListener('click', () => quickWalkRoute(p));
  btns.appendChild(goBtn);

  const nav = document.createElement('a');
  nav.href = 'https://uri.amap.com/marker?position=' + p.location.lng + ',' + p.location.lat +
    '&name=' + encodeURIComponent(p.name);
  nav.target = '_blank';
  nav.rel = 'noopener';
  nav.textContent = '高德导航 ↗';
  btns.appendChild(nav);

  box.appendChild(btns);
  S.infoWindow.setContent(box);
  S.infoWindow.open(S.map, [p.location.lng, p.location.lat]);
}

/* “到这去”:从当前位置步行路线 */
export function quickWalkRoute(p) {
  if (!S.myPos) { toast('还没有定位到你的位置,先点 ⌖ 定位'); return; }
  clearQuickOverlays();
  const walking = new AMap.Walking({});
  walking.search([S.myPos.lng, S.myPos.lat], [p.location.lng, p.location.lat], (status, result) => {
    if (status !== 'complete' || !result.routes || !result.routes.length) {
      toast(explainAmapError(result) || '路线规划失败,距离可能太远');
      return;
    }
    const r = result.routes[0];
    const line = new AMap.Polyline({
      path: flattenRoutePath(r), strokeColor: '#4a7cf7', strokeWeight: 6, strokeOpacity: .9,
      showDir: true, lineJoin: 'round', zIndex: 115,
    });
    S.map.add(line);
    S.quickOverlays.push(line);
    S.map.setFitView([line], false, [70, 130, 70, 70]);
    toast('🚶 步行 ' + fmtDist(r.distance) + ',约 ' + fmtDur(r.time), 4000);
  });
}
function clearQuickOverlays() {
  if (S.quickOverlays.length) S.map.remove(S.quickOverlays);
  S.quickOverlays = [];
}

/* “最近的一个”:厕所/停车场/加油站/医院 一键直达 */
async function gotoNearest() {
  const cfg = MODES[S.currentMode];
  if (!cfg || !cfg.poi) return;
  if (!S.myPos) { toast('先点 ⌖ 定位到你的位置'); return; }
  const doPick = (pois) => {
    if (!pois.length) { toast('附近没有找到,换个分类或位置试试'); return; }
    const best = pois.slice().sort((a, b) => haversine(S.myPos, a.location) - haversine(S.myPos, b.location))[0];
    S.map.setZoomAndCenter(17, [best.location.lng, best.location.lat]);
    openPoiInfo(best, cfg);
    quickWalkRoute(best);
  };
  if (S.lastPois.length) { doPick(S.lastPois); return; }
  const { kw, type } = currentSearchParams(cfg);
  try {
    doPick(await searchNearbyPlaces({ keyword: kw, type, center: S.myPos, radius: 5000, pageSize: 20 }));
  } catch (error) {
    toast(explainAmapError(error && (error.amapResult || error)) || error.message || '地图周边搜索失败');
  }
}

/* 跑步模式辅助:公园/绿道点位(供 route 模块调用) */
export async function drawAssistPois(cfg) {
  const a = cfg.poiAssist;
  const modeAtCall = S.currentMode;
  try {
    const pois = await searchNearbyPlaces({
      keyword: a.kw,
      type: a.type,
      center: S.map.getCenter(),
      radius: 5000,
      pageSize: 15,
    });
    if (S.currentMode !== modeAtCall) return;
    pois.forEach((p) => {
      const mk = new AMap.Marker({
        position: [p.location.lng, p.location.lat],
        content: '<div class="poi-marker" style="--mk:#16a34a">🌳</div>',
        offset: new AMap.Pixel(-17, -30),
        zIndex: 90,
      });
      mk.on('click', () => openPoiInfo(p, cfg));
      S.poiMarkers.push(mk);
    });
    S.map.add(S.poiMarkers);
  } catch (error) {
    if (S.currentMode === modeAtCall) {
      toast(explainAmapError(error && (error.amapResult || error)) || error.message || '附近地点加载失败');
    }
  }
}

/* ---------------- 找中间点(Weiszfeld 几何中位数) ---------------- */
function clearMeetOverlays() {
  if (S.meetOverlays.length) S.map.remove(S.meetOverlays);
  S.meetOverlays = [];
}

async function runMeet() {
  const errEl = $('meet-err');
  errEl.classList.add('hidden');
  const lines = $('meet-input').value.split('\n').map((s) => s.trim()).filter(Boolean);
  if (lines.length < 2) { errEl.textContent = '至少输入两个人的位置'; errEl.classList.remove('hidden'); return; }

  $('btn-meet-go').disabled = true;
  $('btn-meet-go').textContent = '计算中…';
  try {
    const people = [];
    const missing = [];
    for (const line of lines) {
      if (line === '我' || line === '我的位置') {
        if (S.myPos) people.push({ name: '我', lng: S.myPos.lng, lat: S.myPos.lat });
        else missing.push('我(还没定位,点 ⌖)');
        continue;
      }
      const p = await searchPlaceByKeyword(line);
      if (p) people.push({ name: line, lng: p.location.lng, lat: p.location.lat });
      else missing.push(line);
    }
    if (people.length < 2) {
      errEl.textContent = '有效位置不足两个' + (missing.length ? ':没找到 ' + missing.join('、') : '');
      errEl.classList.remove('hidden');
      return;
    }

    const c = geometricMedian(people);
    clearMeetOverlays();
    people.forEach((p) => {
      S.meetOverlays.push(new AMap.Marker({
        position: [p.lng, p.lat],
        content: '<div class="meet-person">👤<span>' + escapeHtml(p.name.slice(0, 4)) + '</span></div>',
        offset: new AMap.Pixel(-16, -34), zIndex: 150,
      }));
      S.meetOverlays.push(new AMap.Polyline({
        path: [[p.lng, p.lat], [c.lng, c.lat]],
        strokeColor: '#94a3b8', strokeWeight: 3, strokeStyle: 'dashed', strokeOpacity: .7, zIndex: 95,
      }));
    });
    S.meetOverlays.push(new AMap.Marker({
      position: [c.lng, c.lat],
      content: '<div class="flagpin">🎯</div>',
      offset: new AMap.Pixel(-15, -30), zIndex: 160,
    }));
    S.map.add(S.meetOverlays);
    $('meet-mask').classList.add('hidden');
    const far = Math.max(...people.map((p) => haversine(c, p)));
    S.map.setZoomAndCenter(far > 8000 ? 12 : far > 3000 ? 14 : 15, [c.lng, c.lat]);  // moveend 自动搜周边美食
    toast('🎯 已找到对大家最公平的中间点,正在搜周边美食…', 3500);
  } finally {
    $('btn-meet-go').disabled = false;
    $('btn-meet-go').textContent = '找中间点';
  }
}
