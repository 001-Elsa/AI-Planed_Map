/* 计划模式:多任务最短路径(两级 TSP)、拖拽调序实时重算、逐站 ETA、保存/载入/分享 */
'use strict';

import { S } from '../state.js';
import { $, escapeHtml, toast } from '../ui/dom.js';
import { API } from '../services/api.js';
import { fmtDist, fmtDur, fmtClock, toXY, copyText } from '../services/format.js';
import { routeLeg, searchNearestPOI } from '../services/amap.js';
import { solveOrder, solveOrderExact } from '../services/algo.js';
import { requireLogin } from '../ui/auth.js';

/* ---------------- 生命周期 ---------------- */
export function activate() {
  $('plan-save-row').classList.add('hidden');
  if (!$('plan-depart').value) {
    const now = new Date();
    $('plan-depart').value = String(now.getHours()).padStart(2, '0') + ':' + String(now.getMinutes()).padStart(2, '0');
  }
  loadPlans();
}

export function clearAll() {
  if (S.planOverlays.length) S.map.remove(S.planOverlays);
  S.planOverlays = [];
}

export function bindPlanUI() {
  document.querySelectorAll('#plan-mode-seg .seg-btn').forEach((b) => {
    b.addEventListener('click', () => {
      S.planTravelMode = b.dataset.tmode;
      document.querySelectorAll('#plan-mode-seg .seg-btn').forEach((x) => x.classList.toggle('active', x === b));
    });
  });
  $('btn-plan-go').addEventListener('click', runPlan);
  $('btn-ai-plan').addEventListener('click', runAIPlan);
  $('btn-plan-save').addEventListener('click', savePlan);
  $('btn-plan-share').addEventListener('click', sharePlan);
  $('btn-plan-restore').addEventListener('click', () => runPlan());
  $('btn-plan-fold').addEventListener('click', () => {
    const folded = $('plan-body').classList.toggle('hidden');
    $('btn-plan-fold').textContent = folded ? '展开' : '收起';
  });
}

async function runAIPlan() {
  if (!requireLogin()) return;
  const text = $('plan-input').value.trim();
  if (!text) { toast('请先描述你的出行需求'); return; }
  const origin = S.myPos || (() => {
    const center = S.map.getCenter();
    return { lng: center.lng, lat: center.lat };
  })();
  const button = $('btn-ai-plan');
  const out = $('plan-result');
  button.disabled = true;
  button.textContent = '理解需求中…';
  clearAll();
  try {
    const mode = S.planTravelMode === 'drive' ? 'driving' : S.planTravelMode === 'ride' ? 'cycling' : 'walking';
    const result = await API.aiPlan({
      text,
      origin,
      transport_mode: mode,
      default_service_duration_minutes: Math.max(0, parseInt($('plan-stay').value, 10) || 0),
    }, crypto.randomUUID());
    if (result.status === 'need_clarification') {
      out.innerHTML = '<div class="ai-card"><b>还需要你确认</b>' +
        result.questions.map((q) => '<p>' + escapeHtml(q.message || q.field) + '</p>').join('') + '</div>';
      return;
    }
    const ordered = result.stops.map((stop) => ({
      task: stop.task.description,
      name: stop.poi.name,
      address: stop.poi.address || '',
      loc: stop.poi.location,
    }));
    const legs = [];
    let previous = origin;
    for (const stop of ordered) {
      legs.push(await routeLeg(previous, stop.loc, S.planTravelMode));
      previous = stop.loc;
    }
    S.lastPlanCtx = { origin, stops: ordered, legs, missed: [] };
    renderPlanResult(origin, ordered, legs, [], result.algorithm === 'exact-permutation', false);
    const banner = document.createElement('div');
    banner.className = result.status === 'infeasible' ? 'ai-card err' : 'ai-card';
    banner.innerHTML = '<b>' + (result.status === 'infeasible' ? '约束冲突' : '规划说明') + '</b><p>' +
      escapeHtml(result.explanation || '') + '</p>' +
      (result.conflicts || []).map((item) => '<p>• ' + escapeHtml(item) + '</p>').join('');
    out.prepend(banner);
    S.lastPlan = {
      text, travelMode: S.planTravelMode, depart: $('plan-depart').value,
      stay: $('plan-stay').value, aiResult: result,
    };
    $('plan-save-row').classList.remove('hidden');
  } catch (err) {
    out.innerHTML = '<div class="err">AI 规划失败：' + escapeHtml(err.message || String(err)) + '</div>';
  } finally {
    button.disabled = false;
    button.textContent = '✨ AI 规划';
  }
}

function travelModeName() {
  return S.planTravelMode === 'drive' ? '驾车' : S.planTravelMode === 'ride' ? '骑行' : '步行';
}

/* ---------------- 主流程 ---------------- */
async function runPlan() {
  const text = $('plan-input').value;
  const lines = text.split('\n').map((s) => s.trim()).filter(Boolean);
  const out = $('plan-result');
  if (!lines.length) { toast('先输入要办的事,每行一件'); return; }
  if (lines.length > 10) { toast('最多支持 10 件事哦'); return; }

  const btn = $('btn-plan-go');
  btn.disabled = true;
  btn.textContent = '规划中…';
  clearAll();
  $('plan-save-row').classList.add('hidden');
  out.innerHTML = '<div class="muted">① 正在为每件事寻找最近的地点…</div>';

  try {
    const origin = S.myPos || (() => { const c = S.map.getCenter(); return { lng: c.lng, lat: c.lat }; })();

    const stops = [];
    const missed = [];
    for (const line of lines) {
      const poi = await searchNearestPOI(line, origin);
      if (poi) stops.push({ task: line, name: poi.name, address: poi.address || '', loc: { lng: poi.location.lng, lat: poi.location.lat } });
      else missed.push(line);
    }
    if (!stops.length) {
      out.innerHTML = '<div class="err">没有搜到任何相关地点,试着写得更具体些,如「超市」「药店」「快递驿站」。</div>';
      return;
    }

    let order, exact = false;
    if (stops.length <= 6) {
      /* 真实路网距离矩阵 + 全排列精确求解 */
      out.innerHTML = '<div class="muted">② 正在计算真实路网距离矩阵(' + travelModeName() + ')…</div>';
      const D = await buildRealMatrix([origin].concat(stops.map((s) => s.loc)), S.planTravelMode);
      order = solveOrderExact(D, stops.length);
      exact = true;
    } else {
      out.innerHTML = '<div class="muted">② 正在计算最短访问顺序…</div>';
      order = solveOrder(origin, stops.map((s) => s.loc));
    }
    const ordered = order.map((i) => stops[i]);

    out.innerHTML = '<div class="muted">③ 正在按' + travelModeName() + '规划路线…</div>';
    const legs = [];
    let prev = origin;
    for (const s of ordered) {
      const leg = await routeLeg(prev, s.loc, S.planTravelMode);
      legs.push(leg);
      prev = s.loc;
    }

    S.lastPlanCtx = { origin, stops: ordered, legs, missed };
    renderPlanResult(origin, ordered, legs, missed, exact, false);
    S.lastPlan = { text, travelMode: S.planTravelMode, depart: $('plan-depart').value, stay: $('plan-stay').value };
    $('plan-save-row').classList.remove('hidden');
    $('btn-plan-restore').classList.add('hidden');
  } catch (err) {
    out.innerHTML = '<div class="err">规划出错:' + escapeHtml(err && err.message || String(err)) + '</div>';
  } finally {
    btn.disabled = false;
    btn.textContent = '最短路径规划';
  }
}

/* 真实路网距离矩阵(并发成对请求,对称近似) */
async function buildRealMatrix(points, tmode) {
  const n = points.length;
  const D = Array.from({ length: n }, () => new Array(n).fill(0));
  const pairs = [];
  for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) pairs.push([i, j]);
  await Promise.all(pairs.map(async ([i, j]) => {
    const leg = await routeLeg(points[i], points[j], tmode);
    D[i][j] = D[j][i] = leg.distance;
  }));
  return D;
}

function legTimeEst(leg, tmode) {
  if (leg.time != null) return leg.time;
  const speed = tmode === 'drive' ? 8.3 : tmode === 'ride' ? 4 : 1.2; // m/s
  return leg.distance / speed;
}

/* ---------------- 渲染(ETA + 打勾 + 调序) ---------------- */
function renderPlanResult(origin, ordered, legs, missed, exact, manual) {
  const out = $('plan-result');
  clearAll();

  S.planOverlays.push(new AMap.Marker({
    position: [origin.lng, origin.lat],
    content: '<div class="num-marker start">起</div>',
    offset: new AMap.Pixel(-15, -15),
    zIndex: 160,
  }));

  let totalD = 0, totalT = 0, hasTime = true;
  const colorPool = ['#4a7cf7', '#16a34a', '#e74c3c', '#9b59b6', '#f59e0b', '#0ea5e9', '#ef4444', '#8b5cf6', '#10b981', '#f97316'];

  ordered.forEach((s, i) => {
    const mk = new AMap.Marker({
      position: [s.loc.lng, s.loc.lat],
      content: '<div class="num-marker">' + (i + 1) + '</div>',
      offset: new AMap.Pixel(-15, -15),
      zIndex: 160,
    });
    mk.on('click', () => {
      S.infoWindow.setContent('<b>' + (i + 1) + '. ' + escapeHtml(s.name) + '</b><br>' +
        escapeHtml(s.task) + '<br>' + escapeHtml(s.address));
      S.infoWindow.open(S.map, [s.loc.lng, s.loc.lat]);
    });
    S.planOverlays.push(mk);

    const leg = legs[i];
    totalD += leg.distance;
    if (leg.time != null) totalT += leg.time; else hasTime = false;
    S.planOverlays.push(new AMap.Polyline({
      path: leg.path,
      strokeColor: colorPool[i % colorPool.length],
      strokeWeight: 6, strokeOpacity: .85,
      strokeStyle: leg.ok ? 'solid' : 'dashed',
      showDir: true, lineJoin: 'round', zIndex: 100,
    }));
  });

  S.map.add(S.planOverlays);
  S.map.setFitView(S.planOverlays, false, [70, 130, 70, 70]);

  /* 出发时间 + 每站停留 → 逐站到达/离开时刻 */
  const stayMin = Math.max(0, parseInt($('plan-stay').value, 10) || 0);
  const departStr = $('plan-depart').value || '';
  let cursor = null;
  if (/^\d{2}:\d{2}$/.test(departStr)) {
    cursor = new Date();
    cursor.setHours(parseInt(departStr.slice(0, 2), 10), parseInt(departStr.slice(3), 10), 0, 0);
  }

  let html = manual
    ? '<div class="muted" style="margin-bottom:4px">✋ 手动调整后的顺序(可点「恢复最优」)</div>'
    : (exact ? '<div class="muted" style="margin-bottom:4px">✅ 已按真实路网距离精确求解最短顺序(可拖拽/▲▼手动调)</div>' : '');
  ordered.forEach((s, i) => {
    const leg = legs[i];
    let eta = '';
    if (cursor) {
      cursor = new Date(cursor.getTime() + legTimeEst(leg, S.planTravelMode) * 1000);
      const arrive = fmtClock(cursor);
      cursor = new Date(cursor.getTime() + stayMin * 60000);
      eta = ' · <b>' + arrive + '</b> 到' + (i < ordered.length - 1 && stayMin ? ',' + fmtClock(cursor) + ' 走' : '');
    }
    html += '<div class="plan-step" draggable="true" data-i="' + i + '"><span class="no">' + (i + 1) + '</span>' +
      '<div style="flex:1"><div><b>' + escapeHtml(s.name) + '</b> <span class="muted">(' + escapeHtml(s.task) + ')</span></div>' +
      '<div class="leg">' + (i === 0 ? '从当前位置 ' : '') + travelModeName() + ' ' + fmtDist(leg.distance) +
      (leg.time != null ? ' · 约 ' + fmtDur(leg.time) : ' · 直线估算') + eta + '</div></div>' +
      '<span class="mv"><button class="mv-btn mv-up" title="上移">▲</button><button class="mv-btn mv-dn" title="下移">▼</button></span>' +
      '<input type="checkbox" class="done-chk" title="办完打勾"></div>';
  });
  html += '<div class="plan-total">合计 ' + fmtDist(totalD) +
    (hasTime ? ' · 约 ' + fmtDur(totalT) : '') + '(' + travelModeName() + ')' +
    (cursor ? ' · 预计 <b>' + fmtClock(cursor) + '</b> 全部办完' : '') + '</div>';
  if (missed.length) {
    html += '<div class="err">没找到地点:' + missed.map(escapeHtml).join('、') + '(写得更具体些再试)</div>';
  }
  out.innerHTML = html;
  out.querySelectorAll('.done-chk').forEach((chk) => {
    chk.addEventListener('change', () => chk.closest('.plan-step').classList.toggle('done', chk.checked));
  });

  /* 手动调序:▲▼ + 拖拽 */
  out.querySelectorAll('.mv-up').forEach((b) => b.addEventListener('click', (e) => {
    e.stopPropagation();
    const i = Number(b.closest('.plan-step').dataset.i);
    moveStop(i, i - 1);
  }));
  out.querySelectorAll('.mv-dn').forEach((b) => b.addEventListener('click', (e) => {
    e.stopPropagation();
    const i = Number(b.closest('.plan-step').dataset.i);
    moveStop(i, i + 1);
  }));
  let dragFrom = null;
  out.querySelectorAll('.plan-step').forEach((el) => {
    el.addEventListener('dragstart', () => { dragFrom = Number(el.dataset.i); el.classList.add('dragging'); });
    el.addEventListener('dragend', () => el.classList.remove('dragging'));
    el.addEventListener('dragover', (e) => { e.preventDefault(); el.classList.add('drag-over'); });
    el.addEventListener('dragleave', () => el.classList.remove('drag-over'));
    el.addEventListener('drop', (e) => {
      e.preventDefault();
      el.classList.remove('drag-over');
      const to = Number(el.dataset.i);
      if (dragFrom != null && dragFrom !== to) moveStop(dragFrom, to);
      dragFrom = null;
    });
  });
}

/* 手动调序 → 按新顺序实时重算路线 */
async function moveStop(from, to) {
  const ctx = S.lastPlanCtx;
  if (!ctx) return;
  const n = ctx.stops.length;
  if (from < 0 || from >= n || to < 0 || to >= n || from === to) return;
  const stops = ctx.stops.slice();
  const [item] = stops.splice(from, 1);
  stops.splice(to, 0, item);

  const out = $('plan-result');
  out.innerHTML = '<div class="muted">✋ 按新顺序重算路线中…</div>';
  const legs = [];
  let prev = ctx.origin;
  for (const s of stops) {
    const leg = await routeLeg(prev, s.loc, S.planTravelMode);
    legs.push(leg);
    prev = s.loc;
  }
  ctx.stops = stops;
  ctx.legs = legs;
  renderPlanResult(ctx.origin, stops, legs, ctx.missed, false, true);
  $('btn-plan-restore').classList.remove('hidden');
}

/* ---------------- 保存 / 载入 / 分享 ---------------- */
async function savePlan() {
  if (!requireLogin() || !S.lastPlan) return;
  const firstLine = S.lastPlan.text.split('\n').map((s) => s.trim()).filter(Boolean)[0] || '我的计划';
  const name = prompt('给这个计划起个名字:', firstLine);
  if (name === null) return;
  try {
    await API.addPlan(name || firstLine, S.lastPlan);
    toast('计划已保存 💾');
    loadPlans();
  } catch (e) { toast(e.message); }
}

export async function loadPlans() {
  const listEl = $('plan-saved-list');
  listEl.innerHTML = '';
  if (!API.user) {
    $('plan-saved-hint').textContent = API.offline ? '后端未启动' : '登录后可保存计划';
    return;
  }
  try {
    const rows = await API.listPlans();
    $('plan-saved-hint').textContent = rows.length ? rows.length + ' 个' : '还没有保存的计划';
    rows.forEach((p) => {
      let data = null;
      try { data = JSON.parse(p.data); } catch (e) { /* noop */ }
      const item = document.createElement('div');
      item.className = 'saved-item';
      item.innerHTML = '<div><b>' + escapeHtml(p.name) + '</b>' +
        '<div class="addr">' + escapeHtml((p.created_at || '').slice(0, 16)) + '</div></div>';
      const btns = document.createElement('div');
      btns.className = 'saved-btns';
      const load = document.createElement('button');
      load.className = 'small-btn';
      load.textContent = '载入';
      load.addEventListener('click', () => {
        if (!data) { toast('计划数据损坏'); return; }
        $('plan-input').value = data.text || '';
        S.planTravelMode = data.travelMode || 'walk';
        if (data.depart) $('plan-depart').value = data.depart;
        if (data.stay != null) $('plan-stay').value = data.stay;
        document.querySelectorAll('#plan-mode-seg .seg-btn').forEach((x) =>
          x.classList.toggle('active', x.dataset.tmode === S.planTravelMode));
        runPlan();
      });
      const del = document.createElement('button');
      del.className = 'small-btn danger';
      del.textContent = '删';
      del.addEventListener('click', async () => {
        if (!confirm('删除计划「' + p.name + '」?')) return;
        try { await API.delPlan(p.id); loadPlans(); } catch (e) { toast(e.message); }
      });
      btns.appendChild(load);
      btns.appendChild(del);
      item.appendChild(btns);
      listEl.appendChild(item);
    });
  } catch (e) {
    $('plan-saved-hint').textContent = e.message;
  }
}

async function sharePlan() {
  if (!requireLogin()) return;
  const ctx = S.lastPlanCtx;
  if (!ctx) { toast('先规划一条路线'); return; }
  try {
    const name = (S.lastPlan && S.lastPlan.text || '').split('\n').map((s) => s.trim()).filter(Boolean)[0] || '出行计划';
    const payload = {
      name,
      travelMode: S.planTravelMode,
      stops: ctx.stops.map((s) => ({ name: s.name, task: s.task, lng: s.loc.lng, lat: s.loc.lat })),
      legs: ctx.legs.map((l) => ({
        distance: l.distance, time: l.time,
        path: l.path.map(toXY).filter((_, i) => i % 2 === 0),   // 抽稀减小体积
      })),
      origin: { lng: ctx.origin.lng, lat: ctx.origin.lat },
    };
    const r = await API.createShare('plan', payload);
    await copyText(location.origin + '/share.html?t=' + r.token);
    toast('计划分享链接已复制 🔗', 3500);
  } catch (e) { toast(e.message); }
}
