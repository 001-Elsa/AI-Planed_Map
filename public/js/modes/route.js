/* 跑步 / 骑行 / 公交模式:多点路线、换乘方案、GPS 实况记录、轨迹回放、记录管理、天气 */
'use strict';

import { S } from '../state.js';
import { $, escapeHtml, toast, speak } from '../ui/dom.js';
import { API } from '../services/api.js?v=33';
import { store } from '../services/store.js';
import { fmtDist, fmtDur, fmtMMSS, haversine, toXY, downloadGPX, copyText } from '../services/format.js';
import { routeLeg, getCity, getLiveWeather, searchTransitPlans } from '../services/amap.js?v=33';
import { requireLogin } from '../ui/auth.js?v=33';
import { MODES } from './registry.js?v=33';
import { drawAssistPois } from './poi.js?v=33';

/* ---------------- 生命周期 ---------------- */
export function activate(cfg) {
  $('route-hint').textContent = cfg.hint;
  $('route-result').classList.add('hidden');
  $('btn-route-save').classList.add('hidden');
  const isTransit = cfg.kind === 'transit';
  $('route-records').classList.toggle('hidden', isTransit);
  $('live-rec').classList.toggle('hidden', !cfg.liveRec);
  if (!isTransit) loadTracks();
  if (cfg.poiAssist) drawAssistPois(cfg);
  if (cfg.weather) loadWeather();
  if (S.rec.active) restoreRecOverlay();
}

export function clearAll() {
  cancelReplay();
  clearRouteLines();
  if (S.wpMarkers.length) S.map.remove(S.wpMarkers);
  S.wpMarkers = [];
  clearTransitOverlays();
}

export function bindRouteUI() {
  $('btn-route-undo').addEventListener('click', undoWaypoint);
  $('btn-route-clear').addEventListener('click', clearRouteMode);
  $('btn-route-go').addEventListener('click', planWaypointRoute);
  $('btn-route-save').addEventListener('click', saveTrack);
  $('btn-route-steps').addEventListener('click', () => $('route-steps-list').classList.toggle('hidden'));

  /* 实况记录 */
  $('btn-rec-start').addEventListener('click', recStart);
  $('btn-rec-pause').addEventListener('click', recPauseResume);
  $('btn-rec-stop').addEventListener('click', recStop);

  /* 语音播报开关 */
  S.voiceOn = store.get('mapgo_voice') !== '0';
  const voiceUI = () => { $('btn-voice').textContent = S.voiceOn ? '🔊 播报开' : '🔇 播报关'; };
  $('btn-voice').addEventListener('click', () => {
    S.voiceOn = !S.voiceOn;
    store.set('mapgo_voice', S.voiceOn ? '1' : '0');
    voiceUI();
    if (S.voiceOn) speak('语音播报已开启');
  });
  voiceUI();
}

/* ---------------- 途经点选择 ---------------- */
export function handleRouteClick(e, cfg) {
  const isTransit = cfg.kind === 'transit';
  if (isTransit && S.waypoints.length >= 2) {
    clearAll();
    S.waypoints = [];
    $('transit-plans').classList.add('hidden');
  }
  S.waypoints.push({ lng: e.lnglat.lng, lat: e.lnglat.lat });
  const idx = S.waypoints.length;
  const mk = new AMap.Marker({
    position: e.lnglat,
    content: '<div class="num-marker sm" style="background:' + cfg.color + '">' +
      (isTransit ? (idx === 1 ? '起' : '终') : idx) + '</div>',
    offset: new AMap.Pixel(-13, -13),
    zIndex: 150,
  });
  S.map.add(mk);
  S.wpMarkers.push(mk);
  $('route-hint').textContent = isTransit
    ? (idx === 1 ? '起点已选,再点终点' : '起终点已选,点「规划路线」')
    : '已选 ' + idx + ' 个点' + (idx >= 2 ? ',可以「规划路线」了' : ',继续点或撤销');
}

function undoWaypoint() {
  if (!S.waypoints.length) return;
  S.waypoints.pop();
  const mk = S.wpMarkers.pop();
  if (mk) S.map.remove(mk);
  clearRouteLines();
  $('route-hint').textContent = S.waypoints.length ? '已选 ' + S.waypoints.length + ' 个点' : MODES[S.currentMode].hint;
}

function clearRouteLines() {
  if (S.routeLines.length) S.map.remove(S.routeLines);
  S.routeLines = [];
  $('route-result').classList.add('hidden');
  $('btn-route-save').classList.add('hidden');
  S.lastTrack = null;
}

function clearRouteMode() {
  clearAll();
  S.waypoints = [];
  S.transitData = null;
  $('transit-plans').classList.add('hidden');
  $('route-steps').classList.add('hidden');
  $('route-steps-list').classList.add('hidden');
  if (MODES[S.currentMode] && MODES[S.currentMode].route) {
    $('route-hint').textContent = MODES[S.currentMode].hint;
  }
}

/* ---------------- 多点路线规划(逐段拼接) ---------------- */
async function planWaypointRoute() {
  const cfg = MODES[S.currentMode];
  if (!cfg || !cfg.route) return;
  if (S.waypoints.length < 2) { toast('至少点两个点才能规划'); return; }
  if (cfg.kind === 'transit') { planTransit(); return; }
  clearRouteLines();
  $('route-hint').textContent = '正在规划路线…';

  const tmode = cfg.kind === 'ride' ? 'ride' : 'walk';
  let totalD = 0, totalT = 0, allPath = [], failed = 0, allInstr = [], lastErr = null;
  for (let i = 0; i < S.waypoints.length - 1; i++) {
    const leg = await routeLeg(S.waypoints[i], S.waypoints[i + 1], tmode);
    totalD += leg.distance;
    if (leg.time != null) totalT += leg.time;
    if (!leg.ok) { failed++; if (leg.err) lastErr = leg.err; }
    allPath = allPath.concat(leg.path);
    allInstr = allInstr.concat(leg.instr || []);
  }
  if (lastErr) toast(lastErr, 4000);

  const line = new AMap.Polyline({
    path: allPath, strokeColor: cfg.color, strokeWeight: 6, strokeOpacity: .9,
    showDir: true, lineJoin: 'round', lineCap: 'round', zIndex: 110,
  });
  S.map.add(line);
  S.routeLines.push(line);
  S.map.setFitView(S.wpMarkers.concat(S.routeLines), false, [70, 130, 70, 70]);

  const box = $('route-result');
  box.classList.remove('hidden');
  if (cfg.kind === 'run') {
    const paceSec = (totalD / 1000) * 6 * 60;
    box.innerHTML =
      '🏃 路线全长 <b>' + fmtDist(totalD) + '</b><br>' +
      '慢跑(6分/公里)约 <b>' + fmtDur(paceSec) + '</b> · ' +
      '快跑(4分半/公里)约 <b>' + fmtDur((totalD / 1000) * 4.5 * 60) + '</b><br>' +
      '<span class="muted">折返跑总程 ' + fmtDist(totalD * 2) + (failed ? ' · 部分路段为直线估算' : '') + '</span>';
    S.lastTrack = { kind: 'run', distance: totalD, duration: paceSec, path: allPath.map(toXY) };
  } else {
    box.innerHTML =
      '🚴 骑行距离 <b>' + fmtDist(totalD) + '</b>,预计 <b>' + fmtDur(totalT) + '</b>' +
      (failed ? '<br><span class="muted">部分路段为直线估算</span>' : '');
    S.lastTrack = { kind: 'ride', distance: totalD, duration: totalT, path: allPath.map(toXY) };
  }
  $('route-hint').textContent = '可继续加点后重新规划,或保存这条路线';
  $('btn-route-save').classList.toggle('hidden', !API.user);
  if (!API.user && !API.offline) toast('登录后可保存路线到我的记录', 2200);

  if (allInstr.length) {
    $('route-steps').classList.remove('hidden');
    $('route-steps-list').innerHTML = allInstr.map((s, i) =>
      '<div class="step-item"><span>' + (i + 1) + '.</span> ' + escapeHtml(s) + '</div>').join('');
  } else {
    $('route-steps').classList.add('hidden');
  }
}

/* ---------------- 公交换乘 ---------------- */
function clearTransitOverlays() {
  if (S.transitOverlays.length) S.map.remove(S.transitOverlays);
  S.transitOverlays = [];
}

async function planTransit() {
  clearTransitOverlays();
  $('route-hint').textContent = '正在规划换乘方案…';
  $('transit-plans').classList.add('hidden');
  const city = await getCity();
  const from = S.waypoints[0], to = S.waypoints[1];
  try {
    S.transitData = await searchTransitPlans(from, to, city);
    if (!S.transitData.length) {
      $('route-hint').textContent = '没有找到换乘方案,距离太近可试试步行/骑行';
      return;
    }
    renderTransitPlans();
    drawTransitPlan(0);
    $('route-hint').textContent = '共 ' + S.transitData.length + ' 个方案,点击切换';
  } catch (error) {
    $('route-hint').textContent = error.message || '公交换乘规划失败';
  }
}

function transitSummary(plan) {
  const lines = [];
  (plan.segments || []).forEach((seg) => {
    if (seg.transit_mode === 'WALK') return;
    const t = seg.transit;
    if (t && t.lines && t.lines.length) lines.push(t.lines[0].name.replace(/\(.*?\)/g, ''));
    else if (seg.instruction) lines.push(String(seg.instruction).slice(0, 12));
  });
  return lines.length ? lines.join(' → ') : '步行';
}

function renderTransitPlans() {
  const box = $('transit-plans');
  box.classList.remove('hidden');
  box.innerHTML = '';
  S.transitData.forEach((plan, i) => {
    const div = document.createElement('div');
    div.className = 'transit-plan' + (i === 0 ? ' active' : '');
    div.innerHTML =
      '<div class="tp-head"><b>' + fmtDur(plan.time) + '</b>' +
      '<span class="muted"> · ' + fmtDist(plan.distance) +
      (plan.cost ? ' · ¥' + plan.cost : '') +
      (plan.nightLine ? ' · 夜班' : '') + '</span></div>' +
      '<div class="tp-lines">' + escapeHtml(transitSummary(plan)) + '</div>';
    div.addEventListener('click', () => {
      box.querySelectorAll('.transit-plan').forEach((x, j) => x.classList.toggle('active', j === i));
      drawTransitPlan(i);
    });
    box.appendChild(div);
  });
}

function drawTransitPlan(i) {
  clearTransitOverlays();
  const plan = S.transitData[i];
  if (!plan) return;
  (plan.segments || []).forEach((seg) => {
    const path = seg.path || (seg.transit && seg.transit.path) || [];
    if (!path.length) return;
    const walk = seg.transit_mode === 'WALK';
    const subway = seg.transit_mode === 'SUBWAY' || seg.transit_mode === 'METRO_RAIL';
    S.transitOverlays.push(new AMap.Polyline({
      path: path,
      strokeColor: walk ? '#64748b' : subway ? '#7c3aed' : '#0d9488',
      strokeWeight: walk ? 4 : 6,
      strokeOpacity: .9,
      strokeStyle: walk ? 'dashed' : 'solid',
      lineJoin: 'round', zIndex: 110,
    }));
  });
  if (S.transitOverlays.length) {
    S.map.add(S.transitOverlays);
    S.map.setFitView(S.transitOverlays.concat(S.wpMarkers), false, [70, 130, 70, 70]);
  }
  const box = $('route-result');
  box.classList.remove('hidden');
  box.innerHTML = '🚌 ' + escapeHtml(transitSummary(plan)) +
    '<br>全程 <b>' + fmtDist(plan.distance) + '</b> · 约 <b>' + fmtDur(plan.time) + '</b>' +
    (plan.cost ? ' · 票价约 ¥' + plan.cost : '');
  const instr = (plan.segments || []).map((s) => s.instruction).filter(Boolean);
  if (instr.length) {
    $('route-steps').classList.remove('hidden');
    $('route-steps-list').innerHTML = instr.map((s, k) =>
      '<div class="step-item"><span>' + (k + 1) + '.</span> ' + escapeHtml(s) + '</div>').join('');
  }
}

/* ---------------- 天气建议 ---------------- */
async function loadWeather() {
  const el = $('route-weather');
  try {
    const city = await getCity();
    if (!city) return;
    const data = await getLiveWeather(city);
    const t = parseFloat(data.temperature);
    const bad = /雨|雪|雷|霾|沙|暴/.test(data.weather || '');
    const nice = !bad && t >= 5 && t <= 32;
    el.classList.remove('hidden');
    el.innerHTML = '🌤 ' + escapeHtml(data.city || city) + ' ' + escapeHtml(data.weather || '') +
      ' ' + escapeHtml(data.temperature || '?') + '℃ · ' +
      escapeHtml(data.windDirection || '') + '风' + escapeHtml(data.windPower || '') + '级 · 湿度' +
      escapeHtml(data.humidity || '?') + '% — ' +
      (nice ? '<b class="good">适合出行运动 👍</b>' : '<b class="bad">今天不太适合,注意安全 ⚠</b>');
  } catch (e) { /* 天气失败不影响主功能 */ }
}

/* ---------------- GPS 实况记录 ---------------- */
function recSetUI() {
  const rec = S.rec;
  $('btn-rec-start').classList.toggle('hidden', rec.active);
  $('btn-rec-pause').classList.toggle('hidden', !rec.active);
  $('btn-rec-stop').classList.toggle('hidden', !rec.active);
  $('rec-stats').classList.toggle('hidden', !rec.active && !rec.points.length);
  $('btn-rec-pause').textContent = rec.paused ? '▶ 继续' : '⏸ 暂停';
}

function recTick() {
  const rec = S.rec;
  if (!rec.active) return;
  const elapsed = (Date.now() - rec.startTs - rec.pausedMs - (rec.paused ? Date.now() - rec.pauseTs : 0)) / 1000;
  $('rec-time').textContent = fmtMMSS(elapsed);
  $('rec-dist').textContent = (rec.dist / 1000).toFixed(2);
  if (rec.dist > 50) {
    const paceSec = elapsed / (rec.dist / 1000);
    $('rec-pace').textContent = Math.floor(paceSec / 60) + "'" + String(Math.round(paceSec % 60)).padStart(2, '0') + '"';
  }
}

function recStart() {
  const rec = S.rec;
  if (rec.active) return;
  if (!navigator.geolocation) { toast('此设备不支持定位'); return; }
  rec.active = true; rec.paused = false;
  rec.points = []; rec.dist = 0; rec.pausedMs = 0;
  rec.startTs = Date.now(); rec.nextKm = 1000; rec.kmMarks = [];
  if (rec.line) { S.map.remove(rec.line); rec.line = null; }
  recSetUI();
  toast('开始记录!把手机带在身上出发吧 🎽', 3000);
  try { navigator.wakeLock && navigator.wakeLock.request('screen').catch(() => {}); } catch (e) { /* noop */ }

  speak('开始记录,加油!');
  rec.watchId = navigator.geolocation.watchPosition((pos) => {
    if (rec.paused) return;
    const elapsedNow = (Date.now() - rec.startTs - rec.pausedMs) / 1000;
    const pt = { lng: pos.coords.longitude, lat: pos.coords.latitude, t: elapsedNow };
    /* 高德底图 GCJ-02 vs 浏览器 GPS WGS-84:轨迹整体偏移不影响里程/形状,GPX 导出原始坐标 */
    const last = rec.points[rec.points.length - 1];
    if (last) {
      const d = haversine(last, pt);
      if (d < 2 || d > 200) return;  // 静止抖动或信号跳变丢弃
      rec.dist += d;
    }
    rec.points.push(pt);
    const path = rec.points.map((p) => [p.lng, p.lat]);
    if (!rec.line) {
      rec.line = new AMap.Polyline({
        path: path, strokeColor: '#ef4444', strokeWeight: 6, strokeOpacity: .95,
        lineJoin: 'round', lineCap: 'round', zIndex: 140,
      });
      S.map.add(rec.line);
    } else rec.line.setPath(path);
    S.map.setCenter([pt.lng, pt.lat]);
    /* 每公里落标 + 语音播报配速 */
    if (rec.dist >= rec.nextKm) {
      const km = Math.round(rec.nextKm / 1000);
      const mk = new AMap.Marker({
        position: [pt.lng, pt.lat],
        content: '<div class="km-marker">' + km + 'km</div>',
        offset: new AMap.Pixel(-16, -12), zIndex: 145,
      });
      S.map.add(mk);
      rec.kmMarks.push(mk);
      rec.nextKm += 1000;
      toast('已完成 ' + km + ' 公里 💪');
      const paceSec = elapsedNow / (rec.dist / 1000);
      speak('已完成 ' + km + ' 公里,用时 ' + Math.floor(elapsedNow / 60) + ' 分 ' + Math.round(elapsedNow % 60) +
        ' 秒,平均配速每公里 ' + Math.floor(paceSec / 60) + ' 分 ' + Math.round(paceSec % 60) + ' 秒');
    }
  }, (err) => {
    toast('定位失败:' + (err.message || '请检查权限') + '(需要 https 或 localhost)', 4000);
  }, { enableHighAccuracy: true, maximumAge: 1000, timeout: 15000 });

  rec.timer = setInterval(recTick, 1000);
}

function recPauseResume() {
  const rec = S.rec;
  if (!rec.active) return;
  if (rec.paused) {
    rec.pausedMs += Date.now() - rec.pauseTs;
    rec.paused = false;
    toast('继续记录');
  } else {
    rec.paused = true;
    rec.pauseTs = Date.now();
    toast('已暂停');
  }
  recSetUI();
}

async function recStop() {
  const rec = S.rec;
  if (!rec.active) return;
  const elapsed = (Date.now() - rec.startTs - rec.pausedMs - (rec.paused ? Date.now() - rec.pauseTs : 0)) / 1000;
  navigator.geolocation.clearWatch(rec.watchId);
  clearInterval(rec.timer);
  rec.active = false; rec.paused = false;
  recSetUI();

  if (rec.dist < 50) {
    toast('里程太短,本次不保存');
    recDiscardOverlay();
    return;
  }
  const kind = MODES[S.currentMode].kind === 'ride' ? 'ride' : 'run';
  const defName = (kind === 'run' ? '实跑 ' : '实骑 ') + fmtDist(rec.dist);
  const summary = fmtDist(rec.dist) + ',用时 ' + fmtDur(elapsed);
  speak('记录结束,' + summary + ',辛苦了!');
  if (API.user) {
    const name = prompt('记录完成!' + summary + '\n起个名字:', defName);
    if (name !== null) {
      try {
        /* 逐点时间戳入库,回放时按真实配速变速 */
        await API.addTrack({ kind, name: name || defName, distance: rec.dist, duration: elapsed, real: true, path: rec.points.map((p) => [p.lng, p.lat, Math.round(p.t || 0)]) });
        toast('已保存 🎽 ' + summary);
        loadTracks();
      } catch (e) { toast(e.message); }
    }
  } else if (confirm('未登录,无法保存到云端。\n' + summary + '\n要导出 GPX 文件留档吗?')) {
    downloadGPX(defName, rec.points.map((p) => [p.lng, p.lat]));
  }
}

function recDiscardOverlay() {
  const rec = S.rec;
  if (rec.line) { S.map.remove(rec.line); rec.line = null; }
  if (rec.kmMarks.length) { S.map.remove(rec.kmMarks); rec.kmMarks = []; }
  rec.points = []; rec.dist = 0;
  recSetUI();
}

function restoreRecOverlay() {
  const rec = S.rec;
  if (rec.line) { try { S.map.add(rec.line); } catch (e) { /* noop */ } }
  if (rec.kmMarks.length) { try { S.map.add(rec.kmMarks); } catch (e) { /* noop */ } }
  recSetUI();
}

/* ---------------- 记录列表 / 查看 / 回放 / 分享 / GPX ---------------- */
export function trackPath(t) {
  let raw = [];
  try { raw = JSON.parse(t.path); } catch (e) { /* noop */ }
  return raw.map((p) => [p[0], p[1]]);
}
function trackTimes(t) {
  let raw = [];
  try { raw = JSON.parse(t.path); } catch (e) { /* noop */ }
  if (raw.length && raw[0].length >= 3) return raw.map((p) => p[2]);
  return null;
}

export async function loadTracks() {
  const cfg = MODES[S.currentMode];
  if (!cfg || !cfg.route || cfg.kind === 'transit') return;
  const listEl = $('route-records-list');
  listEl.innerHTML = '';
  if (!API.user) {
    $('route-records-hint').textContent = API.offline ? '后端未启动' : '登录后可保存与查看';
    return;
  }
  try {
    const rows = await API.listTracks(cfg.kind);
    $('route-records-hint').textContent = rows.length ? rows.length + ' 条' : '还没有记录';
    rows.forEach((t) => {
      const item = document.createElement('div');
      item.className = 'saved-item';
      item.innerHTML =
        '<div><b>' + (t.is_real ? '🎽 ' : '') + escapeHtml(t.name) + '</b>' +
        '<div class="addr">' + fmtDist(t.distance) + (t.duration ? ' · 约 ' + fmtDur(t.duration) : '') +
        (t.is_real ? ' · 实录' : '') +
        ' · ' + escapeHtml((t.created_at || '').slice(0, 16)) + '</div></div>';
      const btns = document.createElement('div');
      btns.className = 'saved-btns';
      const mkBtn = (cls, label, fn, title) => {
        const b = document.createElement('button');
        b.className = cls; b.textContent = label;
        if (title) b.title = title;
        b.addEventListener('click', fn);
        btns.appendChild(b);
      };
      mkBtn('small-btn', '查看', () => viewTrack(t));
      mkBtn('small-btn accent', '▶ 回放', () => replayTrack(t));
      mkBtn('small-btn', '🔗', () => shareTrack(t), '生成分享链接');
      mkBtn('small-btn', 'GPX', () => {
        let p = [];
        try { p = JSON.parse(t.path); } catch (e) { /* noop */ }
        if (downloadGPX(t.name, p)) toast('GPX 已导出'); else toast('无路径数据');
      }, '导出 GPX 轨迹文件');
      mkBtn('small-btn danger', '删', async () => {
        if (!confirm('删除「' + t.name + '」?')) return;
        try { await API.delTrack(t.id); loadTracks(); } catch (e) { toast(e.message); }
      });
      item.appendChild(btns);
      listEl.appendChild(item);
    });
  } catch (e) {
    $('route-records-hint').textContent = e.message;
  }
}

function viewTrack(t) {
  clearAll();
  S.waypoints = [];
  const path = trackPath(t);
  if (!path.length) { toast('这条记录没有路径数据'); return; }
  const cfg = MODES[S.currentMode];
  const line = new AMap.Polyline({
    path: path, strokeColor: cfg.color, strokeWeight: 6, strokeOpacity: .9,
    showDir: true, lineJoin: 'round', zIndex: 110,
  });
  S.map.add(line);
  S.routeLines.push(line);
  S.map.setFitView([line], false, [70, 130, 70, 70]);
  const box = $('route-result');
  box.classList.remove('hidden');
  box.innerHTML = '📂 <b>' + escapeHtml(t.name) + '</b> · ' + fmtDist(t.distance) +
    (t.duration ? ' · 约 ' + fmtDur(t.duration) : '');
  $('route-hint').textContent = '正在查看历史记录,点「清除」回到规划';
}

async function shareTrack(t) {
  if (!requireLogin()) return;
  try {
    let raw = [];
    try { raw = JSON.parse(t.path); } catch (e) { /* noop */ }
    const r = await API.createShare('track', {
      kind: t.kind, name: t.name, distance: t.distance, duration: t.duration, path: raw,
    });
    await copyText(location.origin + '/share.html?t=' + r.token);
    toast('分享链接已复制,发给朋友即可查看 🔗', 3500);
  } catch (e) { toast(e.message); }
}

/* ---------------- 轨迹回放(变速) ---------------- */
export function cancelReplay() {
  const rp = S.replay;
  if (!rp.active && !rp.marker) return;
  if (rp.raf) cancelAnimationFrame(rp.raf);
  if (rp.marker) S.map.remove(rp.marker);
  if (rp.line) S.map.remove(rp.line);
  rp.active = false; rp.raf = 0; rp.marker = null; rp.line = null;
}

function replayTrack(t) {
  clearAll();
  S.waypoints = [];
  const path = trackPath(t);
  if (path.length < 2) { toast('路径点太少,无法回放'); return; }

  /* 每个点的累计秒数:实录有真实时间戳(变速),规划路线按距离均匀合成 */
  let times = trackTimes(t);
  if (!times) {
    const total = t.duration || (t.distance / (t.kind === 'ride' ? 4 : 2));
    times = [0];
    let acc = 0;
    for (let i = 1; i < path.length; i++) {
      acc += haversine({ lng: path[i - 1][0], lat: path[i - 1][1] }, { lng: path[i][0], lat: path[i][1] });
      times.push(acc);
    }
    const dt = acc || 1;
    times = times.map((d) => d / dt * total);
  }
  const realTotal = times[times.length - 1] || 1;
  const playSec = Math.min(40, Math.max(8, realTotal / 60));
  const speedup = realTotal / playSec;

  const base = new AMap.Polyline({ path: path, strokeColor: '#9aa4b8', strokeWeight: 5, strokeOpacity: .55, zIndex: 105 });
  S.map.add(base);
  S.routeLines.push(base);
  const rp = S.replay;
  rp.line = new AMap.Polyline({ path: [path[0]], strokeColor: '#ef4444', strokeWeight: 6, strokeOpacity: .95, lineJoin: 'round', zIndex: 112 });
  S.map.add(rp.line);
  const emoji = t.kind === 'ride' ? '🚴' : '🏃';
  rp.marker = new AMap.Marker({
    position: path[0],
    content: '<div class="runner">' + emoji + '</div>',
    offset: new AMap.Pixel(-16, -26), zIndex: 180,
  });
  S.map.add(rp.marker);
  S.map.setFitView([base], false, [70, 130, 70, 70]);

  rp.active = true;
  const box = $('route-result');
  box.classList.remove('hidden');
  $('route-hint').textContent = '回放中…点「清除」停止';
  const startWall = performance.now();
  let seg = 1;

  const step = (now) => {
    if (!rp.active) return;
    const simT = (now - startWall) / 1000 * speedup;
    if (simT >= realTotal) {
      rp.line.setPath(path);
      rp.marker.setPosition(path[path.length - 1]);
      box.innerHTML = '🏁 回放完成:<b>' + escapeHtml(t.name) + '</b> · ' + fmtDist(t.distance) +
        (t.duration ? ' · 用时 ' + fmtDur(t.duration) : '');
      rp.active = false;
      return;
    }
    while (seg < times.length - 1 && times[seg] <= simT) seg++;
    const i = Math.min(seg, path.length - 1);
    const t0 = times[i - 1], t1 = times[i] != null ? times[i] : t0 + 1;
    const f = Math.min(1, Math.max(0, (simT - t0) / ((t1 - t0) || 1)));
    const p0 = path[i - 1], p1 = path[i];
    const cur = [p0[0] + (p1[0] - p0[0]) * f, p0[1] + (p1[1] - p0[1]) * f];
    rp.marker.setPosition(cur);
    rp.line.setPath(path.slice(0, i).concat([cur]));

    const segDist = haversine({ lng: p0[0], lat: p0[1] }, { lng: p1[0], lat: p1[1] });
    const segTime = (t1 - t0) || 1;
    const paceSec = segDist > 1 ? segTime / (segDist / 1000) : 0;
    box.innerHTML = '▶ 回放中 ×' + Math.round(speedup) +
      ' · <b>' + fmtMMSS(simT) + '</b> / ' + fmtMMSS(realTotal) +
      (paceSec && paceSec < 3600 ? ' · 此刻配速 ' + Math.floor(paceSec / 60) + "'" + String(Math.round(paceSec % 60)).padStart(2, '0') + '"' : '');
    rp.raf = requestAnimationFrame(step);
  };
  rp.raf = requestAnimationFrame(step);
}

async function saveTrack() {
  if (!requireLogin() || !S.lastTrack) return;
  const lt = S.lastTrack;
  const defName = (lt.kind === 'run' ? '跑步 ' : '骑行 ') + fmtDist(lt.distance);
  const name = prompt('给这条路线起个名字:', defName);
  if (name === null) return;
  try {
    await API.addTrack({ kind: lt.kind, name: name || defName, distance: lt.distance, duration: lt.duration, path: lt.path });
    toast('已保存到我的记录 💾');
    loadTracks();
  } catch (e) { toast(e.message); }
}
