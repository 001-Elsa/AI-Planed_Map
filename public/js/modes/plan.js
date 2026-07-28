/* 计划模式:多任务最短路径(两级 TSP)、拖拽调序实时重算、逐站 ETA、保存/载入/分享 */
'use strict';

import { S } from '../state.js';
import { $, escapeHtml, toast } from '../ui/dom.js';
import { API } from '../services/api.js';
import { fmtDist, fmtDur, fmtClock, toXY, copyText } from '../services/format.js';
import { routeLeg, searchNearestPOI } from '../services/amap.js';
import { solveOrder, solveOrderExact } from '../services/algo.js';
import { requireLogin } from '../ui/auth.js';

let activeTripId = null;
let activePlanningRunId = null;
let locationConsentGranted = false;
let locationWatchId = null;
let planningConversationId = null;
let planningConversationRevision = null;
let eventStreamController = null;
let lastStreamEventId = 0;

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
  $('btn-trip-start').addEventListener('click', startTrip);
  $('btn-trip-pause').addEventListener('click', pauseTrip);
  $('btn-location-consent').addEventListener('click', toggleLocationConsent);
  $('btn-trip-replan').addEventListener('click', requestDynamicReplan);
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
    const result = await API.startPlanningConversation({
      text,
      origin,
      transport_mode: mode,
      default_service_duration_minutes: Math.max(0, parseInt($('plan-stay').value, 10) || 0),
    });
    planningConversationId = result.conversation_id;
    planningConversationRevision = result.conversation_revision;
    if (result.status === 'need_clarification') {
      renderClarification(result, out);
      return;
    }
    await renderAIResult(result, origin, text, out);
  } catch (err) {
    out.innerHTML = '<div class="err">AI 规划失败：' + escapeHtml(err.message || String(err)) + '</div>';
  } finally {
    button.disabled = false;
    button.textContent = '✨ AI 规划';
  }
}

function renderClarification(result, out) {
  out.innerHTML = '<div class="ai-card"><b>Agent 发现信息不完整</b>' +
    result.questions.map((q) => '<p><span class="constraint-chip">待补充</span> ' +
      escapeHtml(q.question || q.field) +
      (q.reason ? '<small>' + escapeHtml(q.reason) + '</small>' : '') + '</p>').join('') +
    '<form id="clarification-form" class="clarification-form">' +
    result.questions.map((q, index) => '<label>' + escapeHtml(q.field) +
      '<input name="q-' + index + '" data-field="' + escapeHtml(q.field) +
      '" required placeholder="请输入明确值"></label>').join('') +
    '<button class="primary-btn" type="submit">确认约束并继续规划</button></form></div>';
  $('clarification-form').addEventListener('submit', continueAIConversation);
}

async function continueAIConversation(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const answers = {};
  form.querySelectorAll('input[data-field]').forEach((input) => {
    const field = input.dataset.field;
    let value = input.value.trim();
    if (/meters|minutes|yuan/.test(field)) value = Number(value);
    answers[field] = value;
  });
  const out = $('plan-result');
  try {
    const result = await API.continuePlanningConversation(
      planningConversationId, planningConversationRevision, answers,
    );
    planningConversationRevision = result.conversation_revision;
    if (result.status === 'need_clarification') {
      renderClarification(result, out);
      return;
    }
    const origin = result.origin || S.myPos || S.map.getCenter();
    await renderAIResult(result, origin, $('plan-input').value.trim(), out);
  } catch (error) {
    toast(error.message);
  }
}

async function renderAIResult(result, origin, text, out) {
    const ordered = result.stops.map((stop) => ({
      task: stop.task.description,
      name: stop.poi.name,
      address: stop.poi.address || '',
      loc: stop.poi.location,
      confidence: stop.poi.confidence,
    }));
    const legs = [];
    let previous = origin;
    for (let index = 0; index < ordered.length; index += 1) {
      const stop = ordered[index];
      const visualLeg = await routeLeg(previous, stop.loc, S.planTravelMode);
      const verified = result.stops[index].travel;
      legs.push({
        ...visualLeg,
        distance: verified.distance_meters,
        time: verified.duration_seconds,
        source: verified.source,
        quality: verified.quality,
        confidence: verified.confidence,
        fallbackUsed: verified.fallback_used,
      });
      previous = stop.loc;
    }
    S.lastPlanCtx = { origin, stops: ordered, legs, missed: [] };
    renderPlanResult(origin, ordered, legs, [], result.algorithm === 'exact-permutation', false);
    const banner = document.createElement('div');
    banner.className = result.status === 'infeasible' ? 'ai-card err' : 'ai-card';
    banner.innerHTML = '<b>' + (result.status === 'infeasible' ? '约束冲突' : '规划说明') + '</b><p>' +
      escapeHtml(result.explanation || '') + '</p>' +
      '<div class="plan-proof"><span>置信度 ' + Math.round((result.confidence || 0) * 100) + '%</span>' +
      '<span>候选 ' + (result.candidate_count || 0) + '</span><span>' + escapeHtml(result.algorithm || '') + '</span></div>' +
      (result.uncertainty ? '<p>耗时合理区间 ' +
        fmtDur(result.uncertainty.lower_duration_seconds) + '～' +
        fmtDur(result.uncertainty.upper_duration_seconds) +
        (result.uncertainty.on_time_probability != null ? ' · 按时概率 ' +
          Math.round(result.uncertainty.on_time_probability * 100) + '%' : '') + '</p>' : '') +
      (result.conflicts || []).map((item) => '<p>• ' + escapeHtml(item) + '</p>').join('') +
      (result.warnings || []).map((item) => '<p class="estimate-warning">⚠ ' + escapeHtml(item) + '</p>').join('');
    out.prepend(banner);
    S.lastPlan = {
      text, travelMode: S.planTravelMode, depart: $('plan-depart').value,
      stay: $('plan-stay').value, aiResult: result,
    };
    activePlanningRunId = result.planning_run_id;
    renderStructuredTimeline(result);
    $('plan-save-row').classList.remove('hidden');
}

function renderStructuredTimeline(result) {
  const aside = $('agent-timeline');
  aside.classList.remove('hidden');
  $('trip-state-badge').textContent = result.planning_state || 'PLAN_READY';
  $('timeline-data-dot').className = result.confidence >= .8 ? 'ok' : result.confidence >= .6 ? 'warn' : 'bad';
  $('timeline-proof').innerHTML =
    '<div><b>' + Math.round((result.confidence || 0) * 100) + '%</b><span>综合置信度</span></div>' +
    '<div><b>v' + (result.plan_version || 1) + '</b><span>计划版本</span></div>' +
    '<div><b>' + (result.candidate_count || 0) + '</b><span>候选 POI</span></div>';
  $('structured-timeline').innerHTML = result.stops.map((stop, index) => {
    const sourceClass = stop.travel.fallback_used ? 'estimated' : 'verified';
    return '<article class="timeline-stop"><span class="timeline-no">' + (index + 1) + '</span>' +
      '<div><b>' + escapeHtml(stop.poi.name) + '</b><small>' +
      escapeHtml(new Date(stop.arrival_time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })) +
      ' 到达 · 停留 ' + stop.task.service_duration_minutes + ' 分钟</small>' +
      '<em class="' + sourceClass + '">' + escapeHtml(stop.travel.source) + ' · ' +
      Math.round(stop.travel.confidence * 100) + '%</em></div></article>';
  }).join('');
  const risk = $('agent-risk-card');
  if (result.status === 'infeasible' || (result.warnings || []).length) {
    risk.classList.remove('hidden');
    risk.innerHTML = '<b>' + (result.status === 'infeasible' ? '硬约束冲突' : '可信度提醒') + '</b>' +
      [...(result.conflicts || []), ...(result.warnings || [])]
        .map((item) => '<p>' + escapeHtml(item) + '</p>').join('');
  } else {
    risk.classList.add('hidden');
  }
}

async function startTrip() {
  if (!activePlanningRunId) { toast('请先生成正式 AI 计划'); return; }
  try {
    if (!activeTripId) {
      const created = await API.createTrip(activePlanningRunId);
      activeTripId = created.trip_id;
    }
    const trip = await API.getTrip(activeTripId);
    if (trip.state === 'PLAN_READY' || trip.state === 'PAUSED') {
      await API.transitionTrip(activeTripId, 'ACTIVE_TRIP', '用户在时间线确认开始行程');
    }
    $('trip-state-badge').textContent = 'ACTIVE_TRIP';
    $('btn-trip-start').classList.add('hidden');
    $('btn-trip-pause').classList.remove('hidden');
    $('btn-trip-replan').classList.remove('hidden');
    startTripEventStream();
    toast('随行 Agent 已启动；未授权前不会读取精确位置');
  } catch (error) { toast(error.message); }
}

async function pauseTrip() {
  if (!activeTripId) return;
  try {
    await API.transitionTrip(activeTripId, 'PAUSED', '用户主动暂停');
    $('trip-state-badge').textContent = 'PAUSED';
    $('btn-trip-pause').classList.add('hidden');
    $('btn-trip-start').classList.remove('hidden');
    stopLocationTracking();
    stopTripEventStream();
  } catch (error) { toast(error.message); }
}

async function startTripEventStream() {
  if (!activeTripId || eventStreamController) return;
  eventStreamController = new AbortController();
  try {
    const response = await fetch('/api/companion/trips/' + activeTripId + '/stream', {
      headers: {
        Authorization: 'Bearer ' + API.token,
        'Last-Event-ID': String(lastStreamEventId),
      },
      signal: eventStreamController.signal,
    });
    if (!response.ok || !response.body) throw new Error('事件流连接失败');
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      const frames = buffer.split('\n\n');
      buffer = frames.pop();
      frames.forEach(handleEventFrame);
    }
  } catch (error) {
    if (error.name !== 'AbortError' && eventStreamController) {
      setTimeout(() => { eventStreamController = null; startTripEventStream(); }, 2000);
      return;
    }
  }
  eventStreamController = null;
  if (activeTripId && $('trip-state-badge').textContent === 'ACTIVE_TRIP') {
    setTimeout(startTripEventStream, 2000);
  }
}

function handleEventFrame(frame) {
  const idLine = frame.split('\n').find((line) => line.startsWith('id:'));
  const dataLine = frame.split('\n').find((line) => line.startsWith('data:'));
  if (idLine) lastStreamEventId = Number(idLine.slice(3).trim()) || lastStreamEventId;
  if (!dataLine) return;
  try {
    const event = JSON.parse(dataLine.slice(5).trim());
    $('trip-state-badge').textContent = event.state;
    if (event.decision && event.decision.should_notify) {
      const risk = $('agent-risk-card');
      risk.classList.remove('hidden');
      risk.innerHTML = '<b>实时风险：' + escapeHtml(event.type) + '</b><p>' +
        escapeHtml(event.decision.reason || '') + '</p>';
    }
  } catch (_) { /* 忽略不完整帧，重连会基于事件 ID 恢复 */ }
}

function stopTripEventStream() {
  if (eventStreamController) eventStreamController.abort();
  eventStreamController = null;
}

async function requestDynamicReplan() {
  if (!activeTripId) return;
  const current = S.myPos || (() => {
    const center = S.map.getCenter();
    return { lng: center.lng, lat: center.lat };
  })();
  try {
    const result = await API.replanTrip(activeTripId, {
      current_location: current,
      current_time: new Date().toISOString(),
      completed_stop_ids: [],
      reason: '用户请求基于当前位置和当前时间重新评估',
    });
    renderPatchProposal(result);
  } catch (error) { toast(error.message); }
}

function renderPatchProposal(result) {
  const card = $('plan-patch-card');
  card.classList.remove('hidden');
  if (!result.patch_created) {
    card.innerHTML = '<b>重规划评估</b><p>' +
      escapeHtml(result.status === 'current_order_still_optimal' ? '原计划仍可执行，无需变更。' : '当前没有可行的自动调整。') +
      '</p>' + (result.options || []).map((option) => '<p>• ' + escapeHtml(option.action) + '</p>').join('');
    return;
  }
  const before = result.impact.before || {};
  const after = result.impact.after || {};
  card.innerHTML = '<b>Plan Patch 待确认</b><div class="patch-compare">' +
    '<div><b>原计划 v' + before.plan_version + '</b><span>行程 ' +
    fmtDur(before.total_travel_seconds || 0) + '</span></div>' +
    '<div><b>新方案</b><span>行程 ' + fmtDur(after.total_travel_seconds || 0) +
    '</span><span>' + fmtDist(after.total_distance_meters || 0) + '</span></div></div>' +
    '<p>Agent 只提出变更；确认后仍会由硬约束验证器复算。</p>' +
    '<div class="patch-actions"><button id="btn-patch-accept" class="primary-btn">接受并应用</button>' +
    '<button id="btn-patch-reject" class="small-btn">保留原计划</button></div>';
  $('btn-patch-accept').onclick = () => decidePatch(result.patch_id, true);
  $('btn-patch-reject').onclick = () => decidePatch(result.patch_id, false);
}

async function decidePatch(patchId, accept) {
  try {
    const decision = await API.decidePlanPatch(activePlanningRunId, patchId, accept);
    $('plan-patch-card').innerHTML = '<b>' +
      (accept ? '已验证并应用计划 v' + decision.plan_version : '已拒绝，原计划保持不变') + '</b>';
    if (accept) $('trip-state-badge').textContent = 'ACTIVE_TRIP';
  } catch (error) { toast(error.message); }
}

async function toggleLocationConsent() {
  if (!activeTripId) { toast('请先开始行程'); return; }
  const next = !locationConsentGranted;
  if (next && !confirm('允许本次行程使用精确位置？位置仅短期保存，行程结束后停止跟踪。')) return;
  try {
    await API.setTripConsent(activeTripId, 'precise_location', next);
    locationConsentGranted = next;
    $('btn-location-consent').textContent = '精确定位：' + (next ? '已授权' : '未授权');
    if (next) startLocationTracking(); else stopLocationTracking();
  } catch (error) { toast(error.message); }
}

function startLocationTracking() {
  if (!navigator.geolocation || locationWatchId != null || !activeTripId) return;
  locationWatchId = navigator.geolocation.watchPosition(async (position) => {
    try {
      await API.updateTripLocation(activeTripId, {
        event_id: 'location-' + Date.now() + '-' + Math.round(position.coords.latitude * 1e5),
        location: { lng: position.coords.longitude, lat: position.coords.latitude },
        accuracy_meters: position.coords.accuracy,
        captured_at: new Date(position.timestamp).toISOString(),
      });
    } catch (error) {
      if (error.status === 403 || error.status === 409) stopLocationTracking();
    }
  }, () => {}, { enableHighAccuracy: true, maximumAge: 15000, timeout: 10000 });
}

function stopLocationTracking() {
  if (locationWatchId != null && navigator.geolocation) {
    navigator.geolocation.clearWatch(locationWatchId);
    locationWatchId = null;
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
      (leg.time != null ? ' · 约 ' + fmtDur(leg.time) : ' · 直线估算') + eta +
      (leg.source ? ' <span class="data-badge ' + (leg.fallbackUsed ? 'estimated' : 'verified') + '">' +
        escapeHtml(leg.fallbackUsed ? '估算' : 'Provider') + ' · ' +
        Math.round((leg.confidence || 0) * 100) + '%</span>' : '') + '</div></div>' +
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
