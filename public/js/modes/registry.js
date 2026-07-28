/* 模式注册表与地图核心:MODES 配置、地图初始化、模式切换生命周期、全局 UI 绑定 */
'use strict';

import { S, DEFAULT_CENTER } from '../state.js';
import { $, escapeHtml, toast } from '../ui/dom.js';
import { store } from '../services/store.js';
import { API } from '../services/api.js';
import { searchPlaceSuggestions } from '../services/amap.js';
import { showSetup, setupErr } from '../ui/auth.js';
import * as poi from './poi.js';
import * as route from './route.js';
import * as plan from './plan.js';
import * as social from './social.js';

const LAST_POS_KEY = 'mapgo_last_pos';
let locationSuggestions = [];
let locationSearchTimer = null;
let locationSearchSeq = 0;
let activeLocationIndex = -1;
let locationInputComposing = false;

/* ---------------- 模式配置 ---------------- */
export const MODES = {
  normal: { name: '常规地图', emoji: '🗺️' },

  food: {
    name: '吃货模式', emoji: '🍜', poi: true, color: '#e74c3c',
    style: 'amap://styles/grey', type: '050000', kw0: '美食', ext: 'all', showRating: true,
    chips: [
      { label: '全部', kw: '美食' }, { label: '火锅', kw: '火锅' }, { label: '烧烤', kw: '烧烤' },
      { label: '面馆', kw: '面馆' }, { label: '快餐', kw: '快餐' }, { label: '小吃', kw: '小吃' },
      { label: '奶茶', kw: '奶茶' }, { label: '咖啡', kw: '咖啡' }, { label: '甜品', kw: '甜品' },
      { label: '自助餐', kw: '自助餐' },
    ],
  },

  toilet: {
    name: '厕所模式', emoji: '🚻', poi: true, color: '#3498db',
    style: 'amap://styles/grey', type: '200300', kw0: '公共厕所',
    nearest: '🧭 最近的厕所',
    chips: [
      { label: '公厕', kw: '公共厕所' },
      { label: '商场卫生间', kw: '卫生间', type: '' },
      { label: '加油站厕所', kw: '加油站', type: '010100' },
    ],
  },

  shop: {
    name: '逛街模式', emoji: '🛍️', poi: true, color: '#9b59b6',
    style: 'amap://styles/grey', type: '060000', kw0: '购物',
    chips: [
      { label: '商场', kw: '购物中心', type: '060100' }, { label: '超市', kw: '超市', type: '060400' },
      { label: '便利店', kw: '便利店', type: '060200' }, { label: '服装', kw: '服装店', type: '' },
      { label: '美妆', kw: '化妆品', type: '' }, { label: '数码', kw: '数码', type: '' },
      { label: '书店', kw: '书店', type: '' }, { label: '花鸟市场', kw: '市场', type: '' },
    ],
  },

  park: {
    name: '停车模式', emoji: '🅿️', poi: true, color: '#0ea5e9',
    style: 'amap://styles/grey', type: '150900', kw0: '停车场',
    nearest: '🧭 最近的停车场',
    chips: [
      { label: '全部', kw: '停车场' }, { label: '地下车库', kw: '地下停车场' },
      { label: '路侧停车', kw: '路侧停车' }, { label: '带充电桩', kw: '充电停车场' },
    ],
  },

  fuel: {
    name: '加油充电', emoji: '⛽', poi: true, color: '#f97316',
    style: 'amap://styles/grey', type: '010100', kw0: '加油站',
    nearest: '🧭 最近的一个',
    chips: [
      { label: '加油站', kw: '加油站', type: '010100' },
      { label: '充电站', kw: '充电站', type: '011100' },
      { label: '加气站', kw: '加气站', type: '010300' },
      { label: '洗车', kw: '洗车', type: '' },
    ],
  },

  er: {
    name: '救急模式', emoji: '🏥', poi: true, color: '#dc2626',
    style: 'amap://styles/grey', type: '090100', kw0: '医院',
    nearest: '🧭 最近的一个',
    chips: [
      { label: '医院', kw: '医院', type: '090100' },
      { label: '药店', kw: '药店', type: '090601' },
      { label: '诊所', kw: '诊所', type: '090200' },
      { label: '24h药店', kw: '24小时药店', type: '090601' },
      { label: '急救中心', kw: '急救中心', type: '' },
    ],
  },

  hotel: {
    name: '酒店模式', emoji: '🏨', poi: true, color: '#7c3aed',
    style: 'amap://styles/grey', type: '100000', kw0: '酒店', ext: 'all', showRating: true,
    chips: [
      { label: '全部', kw: '酒店' }, { label: '快捷', kw: '快捷酒店' },
      { label: '星级', kw: '星级酒店', type: '100101' }, { label: '民宿', kw: '民宿' },
      { label: '青旅', kw: '青年旅舍' },
    ],
  },

  run: {
    name: '跑步模式', emoji: '🏃', route: true, kind: 'run', color: '#16a34a',
    style: 'amap://styles/fresh', weather: true, liveRec: true,
    poiAssist: { type: '110100,110101,080100', kw: '公园' },
    hint: '沿想跑的路依次点几个点(可多点连线),然后点「规划路线」;或直接「开始记录」实跑',
  },

  ride: {
    name: '骑行模式', emoji: '🚴', route: true, kind: 'ride', color: '#f59e0b',
    style: 'amap://styles/normal', weather: true, liveRec: true,
    hint: '依次点击起点、途经点、终点(可多点),然后点「规划路线」;或「开始记录」实骑',
  },

  transit: {
    name: '公交模式', emoji: '🚌', route: true, kind: 'transit', color: '#0d9488',
    style: 'amap://styles/normal', weather: true,
    hint: '点起点和终点(2 个点),规划公交/地铁换乘方案',
  },

  plan: { name: '计划模式', emoji: '📝', style: 'amap://styles/normal' },
  foot: { name: '足迹模式', emoji: '📔', foot: true, style: 'amap://styles/whitesmoke' },
  friends: { name: '好友', emoji: '👥', style: 'amap://styles/normal' },
  fav:  { name: '我的收藏', emoji: '⭐', style: 'amap://styles/normal' },
  stats: { name: '数据统计', emoji: '📊', style: 'amap://styles/normal' },
};

/* ---------------- 地图初始化 ---------------- */
export function initMap() {
  if (!window.AMap) { showSetup(true, false); setupErr('高德 API 未就绪,请检查 Key。'); return; }

  const cachedPos = getCachedPosition();
  S.map = new AMap.Map('map', {
    zoom: cachedPos ? 15 : 4,
    center: cachedPos ? [cachedPos.lng, cachedPos.lat] : DEFAULT_CENTER,
    viewMode: '2D',
  });
  S.map.addControl(new AMap.Scale());
  S.infoWindow = new AMap.InfoWindow({ offset: new AMap.Pixel(0, -32) });
  if (cachedPos) {
    S.myPos = cachedPos;
    S.myPosName = cachedPos.name || '';
    drawMyMarker();
  }

  window.addEventListener('error', (ev) => {
    const m = String(ev && ev.message || '');
    if (m.indexOf('INVALID_USER_SCODE') !== -1 || m.indexOf('USERKEY') !== -1) {
      showSetup(true, true);
      setupErr('Key 或安全密钥校验失败,请核对后重新保存。');
    }
  });

  if (!cachedPos) toast('请先在上方输入你的位置，比如小区/地标/地址', 5200);

  S.map.on('moveend', () => {
    const cfg = MODES[S.currentMode];
    if (cfg && cfg.poi) {
      clearTimeout(S.searchTimer);
      S.searchTimer = setTimeout(() => poi.searchPOIsInView(), 450);
    }
  });
  S.map.on('click', onMapClick);

  bindMainUI();
  switchMode('normal');
  setTimeout(social.checkNudge, 4000);
}

function setMyPosition(pos, moveMap, name) {
  S.myPos = { lng: Number(pos.lng), lat: Number(pos.lat) };
  S.myPosName = String(name || '').trim();
  store.set(LAST_POS_KEY, JSON.stringify({ lng: S.myPos.lng, lat: S.myPos.lat, name: S.myPosName, t: Date.now() }));
  drawMyMarker();
  if (moveMap) S.map.setZoomAndCenter(15, [S.myPos.lng, S.myPos.lat]);
}

function addressPart(value) {
  if (Array.isArray(value)) return value.join('');
  return String(value || '').trim();
}

function getPlaceAddress(p) {
  const parts = [p.pname, p.cityname, p.adname, p.address]
    .map(addressPart)
    .filter(Boolean);
  return parts.filter((part, index) => index === 0 || !parts.slice(0, index).some((prev) => prev === part)).join(' · ');
}

function hideLocationSuggestions() {
  const list = $('location-suggestions');
  list.classList.add('hidden');
  $('my-location-input').setAttribute('aria-expanded', 'false');
  activeLocationIndex = -1;
}

function setActiveLocationSuggestion(index) {
  const items = $('location-suggestions').querySelectorAll('.location-suggestion');
  if (!items.length) return;
  activeLocationIndex = (index + items.length) % items.length;
  items.forEach((item, i) => {
    const active = i === activeLocationIndex;
    item.classList.toggle('active', active);
    item.setAttribute('aria-selected', String(active));
  });
  items[activeLocationIndex].scrollIntoView({ block: 'nearest' });
}

function renderLocationSuggestions(items, message) {
  const list = $('location-suggestions');
  locationSuggestions = items;
  activeLocationIndex = -1;
  if (message) {
    list.innerHTML = '<div class="location-suggestions-state">' + escapeHtml(message) + '</div>';
  } else {
    list.innerHTML = items.map((p, index) => {
      const address = getPlaceAddress(p) || '地址信息暂缺';
      return '<button type="button" class="location-suggestion" role="option" data-index="' + index + '">' +
        '<span class="location-suggestion-name">' + escapeHtml(p.name || '未命名地点') + '</span>' +
        '<span class="location-suggestion-address">' + escapeHtml(address) + '</span>' +
      '</button>';
    }).join('');
  }
  list.classList.remove('hidden');
  $('my-location-input').setAttribute('aria-expanded', 'true');
}

function chooseLocationSuggestion(index) {
  const p = locationSuggestions[index];
  if (!p || !p.location) return;
  const input = $('my-location-input');
  const address = getPlaceAddress(p);
  const name = p.name || input.value.trim();
  input.value = address ? name + '，' + address : name;
  hideLocationSuggestions();
  setMyPosition({ lng: p.location.lng, lat: p.location.lat }, true, input.value);
  input.blur();
  toast('已设置我的位置：' + name);
  if (MODES[S.currentMode] && MODES[S.currentMode].poi) {
    clearTimeout(S.searchTimer);
    S.searchTimer = setTimeout(() => poi.searchPOIsInView(), 120);
  }
}

async function loadLocationSuggestions(query, chooseFirst) {
  const kw = String(query || '').trim();
  if (!kw) { hideLocationSuggestions(); return; }
  const seq = ++locationSearchSeq;
  $('btn-my-location-search').disabled = true;
  renderLocationSuggestions([], '正在搜索地址...');
  let items = [];
  try {
    items = await searchPlaceSuggestions(kw, 10);
  } catch (e) {
    if (seq !== locationSearchSeq) return;
    renderLocationSuggestions([], '搜索失败，请检查网络后重试');
    $('btn-my-location-search').disabled = false;
    return;
  }
  if (seq !== locationSearchSeq) return;
  $('btn-my-location-search').disabled = false;
  if (!items.length) {
    renderLocationSuggestions([], '没有找到，请输入更完整的省、市、区或门牌号');
    return;
  }
  renderLocationSuggestions(items);
  if (chooseFirst) chooseLocationSuggestion(0);
}

async function setMyPositionBySearch() {
  const input = $('my-location-input');
  const kw = input.value.trim();
  if (!kw) { toast('请输入你的位置，比如小区/地标/地址'); input.focus(); return; }
  if (locationSuggestions.length && !$('location-suggestions').classList.contains('hidden')) {
    chooseLocationSuggestion(activeLocationIndex >= 0 ? activeLocationIndex : 0);
    return;
  }
  await loadLocationSuggestions(kw, false);
}

function getCachedPosition() {
  try {
    const p = JSON.parse(store.get(LAST_POS_KEY) || 'null');
    if (!p || !Number.isFinite(p.lng) || !Number.isFinite(p.lat)) return null;
    if (Date.now() - Number(p.t || 0) > 7 * 24 * 60 * 60 * 1000) return null;
    return { lng: p.lng, lat: p.lat, name: p.name || '' };
  } catch (e) {
    return null;
  }
}

function drawMyMarker() {
  if (!S.myPos) return;
  if (S.myMarker) { S.myMarker.setPosition([S.myPos.lng, S.myPos.lat]); return; }
  S.myMarker = new AMap.Marker({
    position: [S.myPos.lng, S.myPos.lat],
    content: '<div style="width:16px;height:16px;border-radius:50%;background:#4a7cf7;border:3px solid #fff;box-shadow:0 0 0 6px rgba(74,124,247,.25)"></div>',
    offset: new AMap.Pixel(-8, -8),
    zIndex: 200,
  });
  S.map.add(S.myMarker);
}

/* ---------------- 全局 UI 绑定 ---------------- */
function bindMainUI() {
  document.querySelectorAll('#tabbar .tab').forEach((btn) => {
    btn.addEventListener('click', () => switchMode(btn.dataset.mode));
  });
  $('my-location-search').addEventListener('submit', (e) => {
    e.preventDefault();
    setMyPositionBySearch();
  });
  const locationInput = $('my-location-input');
  locationInput.addEventListener('compositionstart', () => { locationInputComposing = true; });
  locationInput.addEventListener('compositionend', () => {
    locationInputComposing = false;
    clearTimeout(locationSearchTimer);
    locationSearchTimer = setTimeout(() => loadLocationSuggestions(locationInput.value, false), 180);
  });
  locationInput.addEventListener('input', () => {
    if (locationInputComposing) return;
    clearTimeout(locationSearchTimer);
    const kw = locationInput.value.trim();
    if (!kw) { hideLocationSuggestions(); return; }
    locationSuggestions = [];
    hideLocationSuggestions();
    locationSearchTimer = setTimeout(() => loadLocationSuggestions(kw, false), 320);
  });
  locationInput.addEventListener('keydown', (e) => {
    if (locationInputComposing || $('location-suggestions').classList.contains('hidden')) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); setActiveLocationSuggestion(activeLocationIndex + 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActiveLocationSuggestion(activeLocationIndex - 1); }
    else if (e.key === 'Enter' && locationSuggestions.length) {
      e.preventDefault();
      chooseLocationSuggestion(activeLocationIndex >= 0 ? activeLocationIndex : 0);
    }
    else if (e.key === 'Escape') { e.preventDefault(); hideLocationSuggestions(); }
  });
  $('location-suggestions').addEventListener('mousedown', (e) => {
    const item = e.target.closest('.location-suggestion');
    if (!item) return;
    e.preventDefault();
    chooseLocationSuggestion(Number(item.dataset.index));
  });
  document.addEventListener('mousedown', (e) => {
    if (!$('my-location-search').contains(e.target)) hideLocationSuggestions();
  });
  $('btn-locate').addEventListener('click', () => {
    locationInput.focus();
    locationInput.select();
    toast('请输入你的位置，比如小区/地标/地址');
  });
  if (S.myPosName) locationInput.value = S.myPosName;
  $('chk-hide-others').addEventListener('change', applyMapDressing);

  poi.bindPoiUI();
  route.bindRouteUI();
  plan.bindPlanUI();
  social.bindSocialUI();

  /* 夜间模式 */
  S.darkMode = store.get('mapgo_dark') === '1';
  $('btn-dark').addEventListener('click', () => {
    S.darkMode = !S.darkMode;
    store.set('mapgo_dark', S.darkMode ? '1' : '0');
    applyDark();
    applyMapDressing();
  });
  applyDark();
}

function applyDark() {
  document.body.classList.toggle('dark', S.darkMode);
  $('btn-dark').textContent = S.darkMode ? '☀️' : '🌙';
}

export function applyMapDressing() {
  if (!S.map) return;
  const cfg = MODES[S.currentMode];
  let style = cfg.style || 'amap://styles/normal';
  if (S.darkMode && (style === 'amap://styles/normal' || style === 'amap://styles/whitesmoke')) {
    style = 'amap://styles/dark';
  }
  S.map.setMapStyle(style);
  const hide = cfg.poi && $('chk-hide-others').checked;
  S.map.setFeatures(hide ? ['bg', 'road', 'building'] : ['bg', 'road', 'building', 'point']);
}

/* ---------------- 模式切换(生命周期:清理 → 面板 → 激活) ---------------- */
export function switchMode(mode) {
  S.currentMode = mode;
  const cfg = MODES[mode];

  document.querySelectorAll('#tabbar .tab').forEach((b) =>
    b.classList.toggle('active', b.dataset.mode === mode));
  $('mode-emoji').textContent = cfg.emoji;
  $('mode-name').textContent = cfg.name;

  /* 清理各模块覆盖物与状态(实况记录进行中保留其轨迹) */
  poi.clearAll();
  route.clearAll();
  plan.clearAll();
  social.clearAll();
  S.infoWindow.close();
  S.waypoints = [];
  S.lastTrack = null;
  S.transitData = null;
  S.activeChipIdx = 0;
  $('poi-search').value = '';
  $('route-steps').classList.add('hidden');
  $('route-steps-list').classList.add('hidden');
  $('transit-plans').classList.add('hidden');
  $('route-weather').classList.add('hidden');

  /* 面板显隐 */
  $('panel-poi').classList.toggle('hidden', !cfg.poi);
  $('panel-route').classList.toggle('hidden', !cfg.route);
  $('panel-plan').classList.toggle('hidden', mode !== 'plan');
  $('panel-fav').classList.toggle('hidden', mode !== 'fav');
  $('panel-foot').classList.toggle('hidden', mode !== 'foot');
  $('panel-stats').classList.toggle('hidden', mode !== 'stats');
  $('panel-friends').classList.toggle('hidden', mode !== 'friends');
  $('btn-meet').classList.toggle('hidden', mode !== 'food');

  applyMapDressing();

  /* 激活当前模式 */
  if (cfg.poi) poi.activate(cfg);
  if (cfg.route) route.activate(cfg);
  if (mode === 'plan') plan.activate();
  if (mode === 'foot') social.loadCheckins();
  if (mode === 'fav') social.loadFavorites();
  if (mode === 'stats') social.loadStats();
  if (mode === 'friends') social.loadFriends();
}

/* 登录态变化后,刷新依赖登录的数据 */
export function refreshModeData() {
  if (!S.map) return;
  if (S.currentMode === 'fav') social.loadFavorites();
  if (MODES[S.currentMode] && MODES[S.currentMode].route) route.loadTracks();
  if (S.currentMode === 'plan') plan.loadPlans();
  if (S.currentMode === 'friends') social.loadFriends();
}

/* 地图点击分发 */
function onMapClick(e) {
  const cfg = MODES[S.currentMode];
  if (!cfg) return;
  if (cfg.foot) return social.handleFootClick(e);
  if (cfg.route) return route.handleRouteClick(e, cfg);
}
