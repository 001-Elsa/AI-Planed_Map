/* 计划模式:多任务最短路径(两级 TSP)、拖拽调序实时重算、逐站 ETA、保存/载入/分享 */
'use strict';

import { S } from '../state.js';
import { $, escapeHtml, toast } from '../ui/dom.js';
import { API } from '../services/api.js?v=33';
import { store } from '../services/store.js';
import { fmtDist, fmtDur, fmtClock, toXY, copyText } from '../services/format.js';
import { routeLeg, searchNearestPOI } from '../services/amap.js?v=33';
import { solveOrder, solveOrderExact } from '../services/algo.js';
import { requireLogin } from '../ui/auth.js?v=33';

let activeTripId = null;
let activePlanningRunId = null;
let locationConsentGranted = false;
let locationWatchId = null;
let planningConversationId = null;
let planningConversationRevision = null;
let eventStreamController = null;
let lastStreamEventId = 0;
let draftSaveTimer = null;
let plannerCapabilities = null;
let completedStopIds = new Set();
let skippedStopIds = new Set();
const PLAN_DRAFT_KEY = 'mapgo_plan_draft';

/* ---------------- 生命周期 ---------------- */
export function activate() {
  $('plan-save-row').classList.add('hidden');
  if (!$('plan-input').value) $('plan-input').value = store.get(PLAN_DRAFT_KEY) || '';
  if (!$('plan-depart').value) {
    const now = new Date();
    $('plan-depart').value = String(now.getHours()).padStart(2, '0') + ':' + String(now.getMinutes()).padStart(2, '0');
  }
  loadPlans();
  loadPlanOverview();
  loadPlannerCapabilities();
}

async function loadPlannerCapabilities() {
  const badge = $('planner-runtime-badge');
  try {
    plannerCapabilities = await API.planningCapabilities();
    badge.className = plannerCapabilities.configuration_warning
      ? 'planner-runtime-badge'
      : 'planner-runtime-badge ready';
    badge.querySelector('b').textContent = plannerCapabilities.configuration_warning
      ? '需配置高德 Web 服务 Key'
      : plannerCapabilities.map_provider + ' · 后端在线';
    if (!API.user) renderGuestPlanningOverview(plannerCapabilities);
  } catch (error) {
    badge.className = 'planner-runtime-badge offline';
    badge.querySelector('b').textContent = '规划后端不可用';
  }
}

function renderGuestPlanningOverview(capabilities) {
  $('plan-overview-hint').textContent = '后端能力已就绪 · 登录后保存正式版本';
  $('plan-overview-stats').innerHTML =
    '<div><b>' + capabilities.max_tasks + '</b><span>最多任务</span></div>' +
    '<div><b>' + capabilities.max_route_matrix_points + '</b><span>路网矩阵点</span></div>' +
    '<div><b>v∞</b><span>版本可回滚</span></div>';
  $('plan-recent-list').innerHTML = '<div class="planner-guest-note"><b>这里不是聊天框壳子</b>' +
    '<span>' + (capabilities.configuration_warning
      ? escapeHtml(capabilities.configuration_warning) + '。当前后端会明确标记估算数据，不伪装成高德结果。'
      : '登录后会调用 ' + escapeHtml(capabilities.map_provider) +
        ' 核验地点，计算真实路网并由后端约束求解器生成正式计划。') + '</span></div>';
}

export function clearAll() {
  if (S.planOverlays.length) S.map.remove(S.planOverlays);
  S.planOverlays = [];
}

function setTripControls(state) {
  const active = ['ACTIVE_TRIP', 'OFF_ROUTE', 'AT_RISK', 'REPLANNING'].includes(state);
  const resumable = state === 'PAUSED';
  const executable = state === 'PLAN_READY';
  const terminal = ['COMPLETED', 'CANCELLED', 'INFEASIBLE'].includes(state);
  $('btn-trip-start').classList.toggle('hidden', !(executable || resumable));
  $('btn-trip-pause').classList.toggle('hidden', !active);
  $('btn-trip-complete').classList.toggle('hidden', !(active || resumable));
  $('btn-trip-cancel').classList.toggle('hidden', terminal || !active && !resumable && !executable);
  $('btn-trip-replan').classList.toggle('hidden', !active);
}

function switchActiveTrip(nextTripId, trackingEnabled = false) {
  if (activeTripId !== nextTripId) {
    stopLocationTracking();
    stopTripEventStream();
    lastStreamEventId = 0;
    completedStopIds = new Set();
    skippedStopIds = new Set();
  }
  activeTripId = nextTripId;
  locationConsentGranted = Boolean(trackingEnabled);
  $('btn-location-consent').textContent = '精确定位：' +
    (locationConsentGranted ? '已授权' : '未授权');
}

export function bindPlanUI() {
  document.querySelectorAll('#plan-mode-seg .seg-btn').forEach((b) => {
    b.addEventListener('click', () => {
      S.planTravelMode = b.dataset.tmode;
      S.planTravelModeExplicit = true;
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
    $('panel-plan').classList.toggle('plan-collapsed', folded);
    $('btn-plan-fold').textContent = folded ? '展开' : '收起';
    $('btn-plan-fold').setAttribute('aria-label', folded ? '展开计划面板' : '收起计划面板');
    $('btn-plan-fold').setAttribute('aria-expanded', String(!folded));
  });
  $('btn-timeline-fold').addEventListener('click', () => {
    const folded = $('agent-timeline').classList.toggle('timeline-collapsed');
    $('btn-timeline-fold').textContent = folded ? '打开控制台' : '收起';
    $('btn-timeline-fold').setAttribute('aria-label', folded ? '打开实时行程控制台' : '收起实时行程控制台');
    $('btn-timeline-fold').setAttribute('aria-expanded', String(!folded));
  });
  $('btn-trip-start').addEventListener('click', startTrip);
  $('btn-trip-pause').addEventListener('click', pauseTrip);
  $('btn-trip-complete').addEventListener('click', () => finishTrip('COMPLETED'));
  $('btn-trip-cancel').addEventListener('click', () => finishTrip('CANCELLED'));
  $('btn-location-consent').addEventListener('click', toggleLocationConsent);
  $('btn-trip-replan').addEventListener('click', requestDynamicReplan);
  document.querySelectorAll('[data-plan-template]').forEach((button) => {
    button.addEventListener('click', () => {
      $('plan-input').value = button.dataset.planTemplate || '';
      persistPlanDraft();
      $('plan-input').focus();
    });
  });
  $('plan-input').addEventListener('input', () => {
    clearTimeout(draftSaveTimer);
    draftSaveTimer = setTimeout(persistPlanDraft, 250);
  });
  $('plan-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      runAIPlan();
    }
  });
}

function persistPlanDraft() {
  const value = $('plan-input').value.trim();
  if (value) store.set(PLAN_DRAFT_KEY, value);
  else store.del(PLAN_DRAFT_KEY);
}

function planRequestOptions(departureTime) {
  const hard = {};
  const maxWalk = Number($('plan-max-walk').value || 0);
  const budget = Number($('plan-budget').value || 0);
  if (maxWalk > 0) hard.max_walking_meters = maxWalk;
  if (budget > 0) hard.max_total_cost_yuan = budget;
  const latestValue = $('plan-latest').value;
  if (latestValue) {
    const latest = departureTime ? new Date(departureTime) : new Date();
    const [hours, minutes] = latestValue.split(':').map(Number);
    latest.setHours(hours, minutes, 0, 0);
    if (departureTime && latest <= new Date(departureTime)) latest.setDate(latest.getDate() + 1);
    hard.latest_return_time = latest.toISOString();
  }
  return {
    constraints: Object.keys(hard).length ? { hard, uncertain: [] } : null,
    preferences_answers: {
      prefer_high_rating: $('plan-high-rating').checked,
      minimize_walking: maxWalk > 0,
      minimize_cost: budget > 0,
    },
  };
}

function planningCity() {
  const locationName = String(S.myPosName || '');
  const provinceCity = locationName.match(/(?:省|自治区)([\u4e00-\u9fa5]{2,8}市)/);
  if (provinceCity) return provinceCity[1];
  const directCity = locationName.match(/(?:^|[，,\s])([\u4e00-\u9fa5]{2,8}市)/);
  return directCity ? directCity[1] : '';
}

function inferTravelModeFromText(text) {
  if (/(?:公共交通|公交|地铁|轻轨|坐车|换乘)/.test(text)) return 'transit';
  if (/(?:开车|驾车|自驾)/.test(text)) return 'drive';
  if (/(?:骑车|骑行|自行车)/.test(text)) return 'ride';
  if (/(?:步行|走路)/.test(text)) return 'walk';
  return '';
}

function showPlanningExecutionPending() {
  const execution = $('planning-execution');
  execution.classList.remove('hidden');
  execution.innerHTML = '<div class="execution-head"><b>后端规划流水线</b><span>请求已提交，正在核验真实数据…</span></div>' +
    '<div class="execution-stages">' + ['Intent Agent', '核验地点', '计算路网', '确定性求解', 'Critic Agent', '保存版本']
      .map((label, index) => '<div class="execution-stage ' + (index === 0 ? 'attention' : '') + '">' +
        '<b>' + label + '</b><small>' + (index === 0 ? '处理中' : '等待上一步') + '</small></div>').join('') + '</div>';
}

function renderPlanningExecution(result) {
  const execution = $('planning-execution');
  const trace = result.execution;
  if (!trace) { execution.classList.add('hidden'); return; }
  execution.classList.remove('hidden');
  execution.innerHTML = '<div class="execution-head"><b>后端执行凭证</b><span>' +
    escapeHtml(trace.map_provider || 'Map Provider') + ' · ' + escapeHtml(trace.intent_parser || 'parser') +
    ' · ' + Number(trace.latency_ms || 0) + ' ms</span></div><div class="execution-stages">' +
    (trace.stages || []).map((stage) => '<div class="execution-stage ' + escapeHtml(stage.status || '') + '">' +
      '<b>' + escapeHtml(stage.label || stage.key) + '</b><small title="' + escapeHtml(stage.detail || '') + '">' +
      escapeHtml(stage.detail || '') + '</small></div>').join('') + '</div>';
}

async function runAIPlan() {
  if (!requireLogin()) return;
  const text = $('plan-input').value.trim();
  if (!text) { toast('请先描述你的出行需求'); return; }
  const origin = S.myPos || (() => {
    const center = S.map.getCenter();
    return { lng: center.lng, lat: center.lat };
  })();
  const requestOrigin = { lng: Number(origin.lng), lat: Number(origin.lat) };
  const button = $('btn-ai-plan');
  const out = $('plan-result');
  button.disabled = true;
  button.textContent = '后端规划中…';
  clearAll();
  switchActiveTrip(null);
  showPlanningExecutionPending();
  try {
    const inferredMode = inferTravelModeFromText(text);
    if (!S.planTravelModeExplicit && inferredMode) {
      S.planTravelMode = inferredMode;
      document.querySelectorAll('#plan-mode-seg .seg-btn').forEach((item) =>
        item.classList.toggle('active', item.dataset.tmode === inferredMode));
    }
    const mode = {
      drive: 'driving',
      ride: 'cycling',
      transit: 'transit',
      walk: 'walking',
    }[S.planTravelMode] || 'walking';
    const departValue = $('plan-depart').value;
    let departureTime = null;
    if (departValue) {
      const [hours, minutes] = departValue.split(':').map(Number);
      const selected = new Date();
      selected.setHours(hours, minutes, 0, 0);
      departureTime = selected.toISOString();
    }
    const requestOptions = planRequestOptions(departureTime);
    const result = await API.startPlanningConversation({
      text,
      origin: requestOrigin,
      departure_time: departureTime,
      transport_mode: mode,
      city: planningCity() || null,
      default_service_duration_minutes: Math.max(0, parseInt($('plan-stay').value, 10) || 0),
      constraints: requestOptions.constraints,
      preferences_answers: requestOptions.preferences_answers,
    });
    renderPlanningExecution(result);
    planningConversationId = result.conversation_id;
    planningConversationRevision = result.conversation_revision;
    if (result.status === 'need_clarification') {
      renderClarification(result, out);
      return;
    }
    await renderAIResult(result, requestOrigin, text, out);
    loadPlanOverview();
  } catch (err) {
    $('planning-execution').classList.add('hidden');
    out.innerHTML = '<div class="err">AI 规划失败：' + escapeHtml(err.message || String(err)) + '</div>';
  } finally {
    button.disabled = false;
    button.textContent = '生成可执行计划';
  }
}

function clarificationControl(question, index) {
  const field = String(question.field || '');
  const name = 'q-' + index;
  if (question.kind === 'confirmation' || field.startsWith('human_confirmation.')) {
    return '<select name="' + name + '" data-field="' + escapeHtml(field) + '" data-value-type="boolean" required>' +
      '<option value="">请选择</option><option value="true">接受，继续当前方案</option>' +
      '<option value="false">不接受，重新规划</option></select>';
  }
  if (Array.isArray(question.candidates) && question.candidates.length) {
    return '<select name="' + name + '" data-field="' + escapeHtml(field) + '" data-value-type="string" required>' +
      '<option value="">请选择地点</option>' + question.candidates.map((candidate) =>
        '<option value="' + escapeHtml(candidate.id) + '">' + escapeHtml(candidate.name) +
        (candidate.address ? ' · ' + escapeHtml(candidate.address) : '') + '</option>').join('') + '</select>';
  }
  if (field === 'origin') {
    return '<div class="clarification-coordinate">' +
      '<input name="' + name + '-lng" data-field="origin.lng" data-value-type="number" required placeholder="经度">' +
      '<input name="' + name + '-lat" data-field="origin.lat" data-value-type="number" required placeholder="纬度"></div>';
  }
  if (/minimize_|wheelchair_accessible$|has_luggage$/.test(field)) {
    return '<select name="' + name + '" data-field="' + escapeHtml(field) + '" data-value-type="boolean" required>' +
      '<option value="">请选择</option><option value="true">是</option><option value="false">否</option></select>';
  }
  if (/dietary_restrictions|areas$/.test(field)) {
    return '<input name="' + name + '" data-field="' + escapeHtml(field) + '" data-value-type="list" required placeholder="多个值请用逗号分隔">';
  }
  if (/meters|minutes|yuan|\.adults$|\.elderly$|\.children$|wheelchair_users$|\.pets$/.test(field)) {
    return '<input type="number" name="' + name + '" data-field="' + escapeHtml(field) + '" data-value-type="number" required>';
  }
  if (/time$/.test(field)) {
    return '<input type="datetime-local" name="' + name + '" data-field="' + escapeHtml(field) + '" data-value-type="datetime" required>';
  }
  return '<input name="' + name + '" data-field="' + escapeHtml(field) + '" data-value-type="string" required placeholder="请输入明确值">';
}

function renderClarification(result, out) {
  out.innerHTML = '<div class="ai-card"><b>Agent 发现信息不完整</b>' +
    result.questions.map((q) => '<p><span class="constraint-chip">待补充</span> ' +
      escapeHtml(q.question || q.field) +
      (q.reason ? '<small>' + escapeHtml(q.reason) + '</small>' : '') + '</p>').join('') +
    '<form id="clarification-form" class="clarification-form">' +
    result.questions.map((q, index) => '<label>' + escapeHtml(q.field) +
      clarificationControl(q, index) + '</label>').join('') +
    '<button class="primary-btn" type="submit">确认约束并继续规划</button></form></div>';
  $('clarification-form').addEventListener('submit', continueAIConversation);
}

async function continueAIConversation(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const answers = {};
  form.querySelectorAll('[data-field]').forEach((input) => {
    const field = input.dataset.field;
    let value = input.value.trim();
    const valueType = input.dataset.valueType;
    if (valueType === 'number') value = Number(value);
    else if (valueType === 'boolean') value = value === 'true';
    else if (valueType === 'list') value = value.split(/[,，、]/).map((item) => item.trim()).filter(Boolean);
    else if (valueType === 'datetime') value = new Date(value).toISOString();
    if (field === 'origin.lng' || field === 'origin.lat') {
      answers.origin = answers.origin || {};
      answers.origin[field.endsWith('.lng') ? 'lng' : 'lat'] = value;
    } else {
      answers[field] = value;
    }
  });
  const out = $('plan-result');
  try {
    const result = await API.continuePlanningConversation(
      planningConversationId, planningConversationRevision, answers,
    );
    renderPlanningExecution(result);
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
    const visualLegs = await Promise.all(ordered.map((stop, index) => {
      const previous = index === 0 ? origin : ordered[index - 1].loc;
      return routeLeg(previous, stop.loc, S.planTravelMode, planningCity());
    }));
    const legs = ordered.map((_stop, index) => {
      const visualLeg = visualLegs[index];
      const verified = result.stops[index].travel;
      return {
        ...visualLeg,
        distance: verified.distance_meters,
        time: verified.duration_seconds,
        source: verified.source,
        quality: verified.quality,
        confidence: verified.confidence,
        fallbackUsed: verified.fallback_used,
      };
    });
    S.lastPlanCtx = { origin, stops: ordered, legs, missed: [] };
    try {
      renderPlanResult(
        origin,
        ordered,
        legs,
        [],
        result.algorithm === 'joint-exact-enumeration',
        false,
      );
    } catch (renderError) {
      clearAll();
      out.innerHTML = '<div class="estimate-warning">正式计划已保存，但地图图层暂时无法绘制。' +
        '可以稍后重新打开；这不会再次创建计划。</div>';
    }
    const banner = document.createElement('div');
    banner.className = result.status === 'infeasible' ? 'ai-card err' : 'ai-card';
    banner.innerHTML = '<b>' + (result.status === 'infeasible' ? '约束冲突' : '规划说明') + '</b><p>' +
      escapeHtml(result.explanation || '') + '</p>' +
      '<div class="plan-proof"><span>置信度 ' + Math.round((result.confidence || 0) * 100) + '%</span>' +
      '<span>候选 ' + (result.candidate_count || 0) + '</span><span>' + escapeHtml(result.algorithm || '') + '</span></div>' +
      (result.uncertainty ? '<p>耗时合理区间 ' +
        fmtDur(result.uncertainty.lower_duration_seconds) + '～' +
        fmtDur(result.uncertainty.upper_duration_seconds) +
        (result.uncertainty.on_time_probability != null ? ' · 启发式按时置信度 ' +
          Math.round(result.uncertainty.on_time_probability * 100) + '%' : '') + '</p>' : '') +
      (result.conflicts || []).map((item) => '<p>• ' + escapeHtml(item) + '</p>').join('') +
      (result.warnings || []).map((item) => '<p class="estimate-warning">⚠ ' + escapeHtml(item) + '</p>').join('');
    out.prepend(banner);
    renderPlanInsights(result, out);
    S.lastPlan = {
      text, travelMode: S.planTravelMode, depart: $('plan-depart').value,
      stay: $('plan-stay').value, aiResult: result,
    };
    activePlanningRunId = result.planning_run_id;
    persistPlanDraft();
    renderStructuredTimeline(result);
    $('plan-save-row').classList.remove('hidden');
}

function constraintSummary(result) {
  const hard = result.intent && result.intent.constraints && result.intent.constraints.hard || {};
  const labels = [];
  if (hard.latest_return_time) labels.push('最晚 ' + new Date(hard.latest_return_time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }));
  if (hard.max_walking_meters != null) labels.push('步行 ≤ ' + fmtDist(hard.max_walking_meters));
  if (hard.max_total_cost_yuan != null) labels.push('预算 ≤ ¥' + hard.max_total_cost_yuan);
  if (hard.wheelchair_accessible) labels.push('无障碍');
  const preferences = result.intent && result.intent.preferences || {};
  if (preferences.prefer_high_rating) labels.push('优先高评分');
  if (preferences.minimize_distance) labels.push('少绕路');
  if (preferences.optimization_goal === 'shortest_time') labels.push('最短时间');
  if (preferences.optimization_goal === 'shortest_distance') labels.push('最短距离');
  return labels;
}

function renderPlanInsights(result, out) {
  const tasks = result.intent && result.intent.tasks || [];
  const reviews = result.candidate_reviews || [];
  const constraints = constraintSummary(result);
  const score = result.score || {};
  const scoreItems = [
    ['行驶时间', score.travel_time], ['步行成本', score.walking_time],
    ['路线距离', score.distance], ['评分偏好', score.low_rating], ['不确定性', score.uncertainty],
  ].filter((item) => Number(item[1]) > 0);
  const maxScore = Math.max(1, ...scoreItems.map((item) => Number(item[1])));
  const section = document.createElement('div');
  section.className = 'plan-insight-grid';
  section.innerHTML = '<section class="plan-insight"><b>后端理解的任务与硬约束</b>' +
    '<div class="candidate-pills">' + (constraints.length
      ? constraints.map((item) => '<span>' + escapeHtml(item) + '</span>').join('')
      : '<span>未设置额外硬约束</span>') + '</div><div class="intent-task-list">' +
    tasks.map((task, index) => '<div class="intent-task"><b>' + (index + 1) + '. ' +
      escapeHtml(task.description) + '</b><small>' +
      escapeHtml(task.location_name || task.category || '由 Provider 匹配地点') +
      ' · 停留 ' + task.service_duration_minutes + ' 分钟</small></div>').join('') + '</div></section>' +
    '<section class="plan-insight"><b>真实候选地点核验</b><div class="candidate-review-list">' +
    (reviews.length ? reviews.map((review) => '<div class="candidate-review"><b>' +
      escapeHtml(review.task_description) + ' · 比较 ' + review.considered_count + ' 个</b><div class="candidate-pills">' +
      (review.candidates || []).slice(0, 4).map((candidate) => '<span class="' +
        (candidate.id === review.selected_poi_id ? 'selected' : '') + '" title="' +
        escapeHtml(candidate.address || '') + '">' + escapeHtml(candidate.name) +
        (candidate.rating != null ? ' ' + candidate.rating + '分' : '') + '</span>').join('') +
      '</div></div>').join('') : '<div class="candidate-review">等待地点核验</div>') + '</div></section>' +
    (scoreItems.length ? '<section class="plan-insight"><b>求解器评分构成</b><div class="score-bars">' +
      scoreItems.map((item) => '<div class="score-bar"><span>' + item[0] + '</span><i style="--score-width:' +
        Math.max(3, Math.round(Number(item[1]) / maxScore * 100)) + '%"></i><b>' +
        Math.round(Number(item[1])) + '</b></div>').join('') + '</div></section>' : '') +
    '<section class="plan-insight"><b>数据可信度</b><div class="candidate-pills">' +
      '<span class="selected">Provider 路线 ' + Number(result.execution && result.execution.verified_route_edges || 0) + ' 段</span>' +
      '<span>估算路线 ' + Number(result.execution && result.execution.estimated_route_edges || 0) + ' 段</span>' +
      '<span>综合 ' + Math.round((result.confidence || 0) * 100) + '%</span></div></section>';
  out.appendChild(section);
}

function renderStructuredTimeline(result) {
  const aside = $('agent-timeline');
  aside.classList.remove('hidden');
  $('trip-state-badge').textContent = result.planning_state || 'PLAN_READY';
  setTripControls(result.status === 'success' ? (result.planning_state || 'PLAN_READY') : 'INFEASIBLE');
  $('timeline-data-dot').className = result.confidence >= .8 ? 'ok' : result.confidence >= .6 ? 'warn' : 'bad';
  $('timeline-proof').innerHTML =
    '<div><b>' + Math.round((result.confidence || 0) * 100) + '%</b><span>综合置信度</span></div>' +
    '<div><b>v' + (result.plan_version || 1) + '</b><span>计划版本</span></div>' +
    '<div><b>' + (result.candidate_count || 0) + '</b><span>候选 POI</span></div>';
  $('structured-timeline').innerHTML = result.stops.map((stop, index) => {
    const sourceClass = stop.travel.fallback_used ? 'estimated' : 'verified';
    return '<article class="timeline-stop" data-stop-id="' + escapeHtml(stop.poi.id) +
      '"><span class="timeline-no">' + (index + 1) + '</span>' +
      '<div><b>' + escapeHtml(stop.poi.name) + '</b><small>' +
      escapeHtml(new Date(stop.arrival_time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })) +
      ' 到达 · 停留 ' + stop.task.service_duration_minutes + ' 分钟</small>' +
      '<em class="' + sourceClass + '">' + escapeHtml(stop.travel.source) + ' · ' +
      Math.round(stop.travel.confidence * 100) + '%</em>' +
      '<span class="timeline-stop-actions"><button type="button" class="small-btn btn-stop-complete" ' +
      'data-stop-id="' + escapeHtml(stop.poi.id) + '" data-planned-arrival="' +
      escapeHtml(stop.arrival_time) + '">完成</button><button type="button" class="small-btn btn-stop-skip" ' +
      'data-stop-id="' + escapeHtml(stop.poi.id) + '">跳过</button></span>' +
      '<span class="timeline-stop-status hidden"></span></div></article>';
  }).join('');
  $('structured-timeline').querySelectorAll('.btn-stop-complete, .btn-stop-skip').forEach((button) => {
    button.addEventListener('click', () => reportStopOutcome(button));
  });
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

function setTripFeedback(message, tone = '') {
  const feedback = $('trip-action-feedback');
  feedback.textContent = message || '';
  feedback.className = 'trip-action-feedback' + (tone ? ' ' + tone : '');
}

function findTimelineStop(stopId) {
  return [...$('structured-timeline').querySelectorAll('.timeline-stop')]
    .find((stop) => stop.dataset.stopId === stopId) || null;
}

function paintStopOutcome(stopId, outcome) {
  const stop = findTimelineStop(stopId);
  if (!stop) return;
  const completed = outcome === 'completed';
  const skipped = outcome === 'skipped';
  stop.classList.toggle('done', completed);
  stop.classList.toggle('skipped', skipped);
  const status = stop.querySelector('.timeline-stop-status');
  status.textContent = completed ? '✓ 已完成并同步' : skipped ? '↷ 已跳过并同步' : '';
  status.classList.toggle('hidden', !outcome);
  stop.querySelectorAll('.timeline-stop-actions button').forEach((item) => {
    item.disabled = Boolean(outcome);
  });
  const completeButton = stop.querySelector('.btn-stop-complete');
  const skipButton = stop.querySelector('.btn-stop-skip');
  completeButton.textContent = completed ? '已完成' : '完成';
  skipButton.textContent = skipped ? '已跳过' : '跳过';
}

function applyTripSummary(summary) {
  completedStopIds = new Set();
  skippedStopIds = new Set(summary.skipped_stop_ids || []);
  (summary.stop_deviations || []).forEach((stop) => {
    if (stop.completed) completedStopIds.add(stop.stop_id);
    if (stop.skipped) skippedStopIds.add(stop.stop_id);
    paintStopOutcome(stop.stop_id, stop.skipped ? 'skipped' : stop.completed ? 'completed' : '');
  });
  const plannedStops = Number(summary.planned_stops || 0);
  const processedStops = new Set([...completedStopIds, ...skippedStopIds]).size;
  const hasRemainingStops = processedStops < plannedStops;
  const replanButton = $('btn-trip-replan');
  replanButton.disabled = !hasRemainingStops;
  replanButton.textContent = hasRemainingStops ? '评估并重规划' : '没有剩余站点';
  setTripFeedback(
    '后端已同步：完成 ' + completedStopIds.size + ' 站，跳过 ' + skippedStopIds.size +
    ' 站，共 ' + plannedStops + ' 站',
    'success',
  );
}

async function hydrateTripRuntime() {
  if (!activeTripId) return;
  const expectedTripId = activeTripId;
  try {
    const [trip, summary] = await Promise.all([
      API.getTrip(expectedTripId),
      API.getTripSummary(expectedTripId),
    ]);
    if (activeTripId !== expectedTripId) return;
    switchActiveTrip(trip.id, trip.tracking_enabled);
    $('trip-state-badge').textContent = trip.state;
    setTripControls(trip.state);
    applyTripSummary(summary);
  } catch (error) {
    if (activeTripId === expectedTripId) setTripFeedback('行程状态同步失败：' + error.message, 'error');
  }
}

async function startTrip() {
  if (!activePlanningRunId) { toast('请先生成正式 AI 计划'); return; }
  try {
    if (!activeTripId) {
      const created = await API.createTrip(activePlanningRunId);
      switchActiveTrip(created.trip_id);
    }
    const trip = await API.getTrip(activeTripId);
    switchActiveTrip(trip.id, trip.tracking_enabled);
    if (trip.state === 'PLAN_READY' || trip.state === 'PAUSED') {
      await API.transitionTrip(activeTripId, 'ACTIVE_TRIP', '用户在时间线确认开始行程');
    }
    $('trip-state-badge').textContent = 'ACTIVE_TRIP';
    setTripControls('ACTIVE_TRIP');
    await hydrateTripRuntime();
    startTripEventStream();
    toast('随行 Agent 已启动；未授权前不会读取精确位置');
  } catch (error) { toast(error.message); }
}

async function pauseTrip() {
  if (!activeTripId) return;
  try {
    await API.transitionTrip(activeTripId, 'PAUSED', '用户主动暂停');
    $('trip-state-badge').textContent = 'PAUSED';
    setTripControls('PAUSED');
    stopLocationTracking();
    stopTripEventStream();
  } catch (error) { toast(error.message); }
}

async function finishTrip(targetState) {
  if (!activeTripId) return;
  const label = targetState === 'COMPLETED' ? '完成' : '取消';
  if (!confirm('确认' + label + '本次行程？')) return;
  try {
    await API.transitionTrip(activeTripId, targetState, '用户主动' + label + '行程');
    $('trip-state-badge').textContent = targetState;
    setTripControls(targetState);
    stopLocationTracking();
    stopTripEventStream();
    const summary = await API.getTripSummary(activeTripId);
    toast('行程已' + label + ' · 完成 ' + summary.completed_stops + '/' + summary.planned_stops + ' 站');
  } catch (error) { toast(error.message); }
}

async function reportStopOutcome(button) {
  if (!activeTripId) { setTripFeedback('请先开始行程', 'error'); toast('请先开始行程'); return; }
  const skipped = button.classList.contains('btn-stop-skip');
  const now = new Date().toISOString();
  const stopId = button.dataset.stopId;
  const actionButtons = [...button.closest('.timeline-stop-actions').querySelectorAll('button')];
  actionButtons.forEach((item) => { item.disabled = true; });
  const originalText = button.textContent;
  button.textContent = skipped ? '正在跳过…' : '正在完成…';
  setTripFeedback((skipped ? '正在跳过：' : '正在完成：') +
    (button.closest('.timeline-stop').querySelector('b').textContent || stopId));
  try {
    await API.sendTripEvent(activeTripId, {
      event_id: ((skipped ? 'stop-skip-' : 'stop-complete-') + stopId).slice(0, 100),
      type: skipped ? 'PlanStopSkipped' : 'PlanStopCompleted',
      occurred_at: now,
      payload: {
        stop_id: stopId,
        planned_arrival: button.dataset.plannedArrival || null,
        arrived_at: skipped ? null : now,
      },
    });
    if (skipped) {
      skippedStopIds.add(stopId);
      completedStopIds.delete(stopId);
    } else {
      completedStopIds.add(stopId);
      skippedStopIds.delete(stopId);
    }
    paintStopOutcome(stopId, skipped ? 'skipped' : 'completed');
    const summary = await API.getTripSummary(activeTripId);
    applyTripSummary(summary);
    toast(skipped ? '该站已跳过并保存' : '该站已完成并保存');
  } catch (error) {
    button.textContent = originalText;
    actionButtons.forEach((item) => { item.disabled = false; });
    setTripFeedback('操作失败：' + error.message, 'error');
    toast(error.message);
  }
}

async function startTripEventStream() {
  if (!activeTripId || eventStreamController) return;
  const streamTripId = activeTripId;
  const controller = new AbortController();
  eventStreamController = controller;
  try {
    const response = await fetch('/api/companion/trips/' + streamTripId + '/stream', {
      headers: {
        Authorization: 'Bearer ' + API.token,
        'Last-Event-ID': String(lastStreamEventId),
      },
      signal: controller.signal,
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
      frames.forEach((frame) => handleEventFrame(frame, streamTripId));
    }
  } catch (error) {
    if (error.name !== 'AbortError' && !controller.signal.aborted && activeTripId === streamTripId) {
      if (eventStreamController === controller) eventStreamController = null;
      setTimeout(() => {
        if (activeTripId === streamTripId) startTripEventStream();
      }, 2000);
      return;
    }
  }
  if (eventStreamController === controller) eventStreamController = null;
  if (activeTripId === streamTripId && $('trip-state-badge').textContent === 'ACTIVE_TRIP') {
    setTimeout(() => {
      if (activeTripId === streamTripId) startTripEventStream();
    }, 2000);
  }
}

function handleEventFrame(frame, streamTripId) {
  if (activeTripId !== streamTripId) return;
  const idLine = frame.split('\n').find((line) => line.startsWith('id:'));
  const dataLine = frame.split('\n').find((line) => line.startsWith('data:'));
  if (idLine) lastStreamEventId = Number(idLine.slice(3).trim()) || lastStreamEventId;
  if (!dataLine) return;
  try {
    const event = JSON.parse(dataLine.slice(5).trim());
    if (event.state) {
      $('trip-state-badge').textContent = event.state;
      setTripControls(event.state);
      if (['COMPLETED', 'CANCELLED'].includes(event.state)) {
        stopLocationTracking();
        stopTripEventStream();
      }
    }
    if (event.decision && event.decision.should_notify) {
      const risk = $('agent-risk-card');
      risk.classList.remove('hidden');
      risk.innerHTML = '<b>实时风险：' + escapeHtml(event.type) + '</b><p>' +
        escapeHtml(event.decision.reason || '') + '</p>';
    }
    if (event.plan_patch && event.plan_patch.patch_created) {
      renderPatchProposal(event.plan_patch);
    }
  } catch (_) { /* 忽略不完整帧；重连只恢复最新状态快照，不保证逐条重放。 */ }
}

function stopTripEventStream() {
  if (eventStreamController) eventStreamController.abort();
  eventStreamController = null;
}

async function requestDynamicReplan() {
  if (!activeTripId) { setTripFeedback('请先开始行程', 'error'); return; }
  const button = $('btn-trip-replan');
  if (button.disabled) {
    setTripFeedback('所有站点都已完成或跳过，没有需要重规划的剩余站点。', 'success');
    return;
  }
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = '正在评估…';
  setTripFeedback('正在读取已完成站点并重新计算剩余行程…');
  let keepDisabled = false;
  const current = S.myPos || (() => {
    const center = S.map.getCenter();
    return { lng: center.lng, lat: center.lat };
  })();
  try {
    const summary = await API.getTripSummary(activeTripId);
    applyTripSummary(summary);
    if (button.disabled) {
      keepDisabled = true;
      setTripFeedback('所有站点都已完成或跳过，没有需要重规划的剩余站点。', 'success');
      return;
    }
    button.disabled = true;
    button.textContent = '正在评估…';
    const result = await API.replanTrip(activeTripId, {
      current_location: current,
      current_time: new Date().toISOString(),
      completed_stop_ids: [...completedStopIds, ...skippedStopIds],
      reason: '用户请求基于当前位置和当前时间重新评估',
    });
    renderPatchProposal(result);
    setTripFeedback(result.patch_created
      ? '后端已生成新的剩余行程方案，请确认是否应用。'
      : '后端评估完成：当前计划无需变更。', 'success');
    toast(result.patch_created ? '重规划方案已生成' : '评估完成，当前计划仍可执行');
  } catch (error) {
    setTripFeedback('重规划失败：' + error.message, 'error');
    toast(error.message);
  } finally {
    if (!keepDisabled) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

function renderPatchProposal(result) {
  const card = $('plan-patch-card');
  card.classList.remove('hidden');
  if (!result.patch_created) {
    card.innerHTML = '<b>重规划评估</b><p>' +
      escapeHtml(result.status === 'current_plan_still_feasible'
        ? '原计划仍可执行，无需变更。'
        : '当前没有可行的自动调整。') +
      '</p>' + (result.options || result.alternatives || []).map((option) => '<p>• ' +
        escapeHtml(option.action || option.label || option.transport_mode || '') + '</p>').join('');
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
  if (!activeTripId) { setTripFeedback('请先开始行程', 'error'); toast('请先开始行程'); return; }
  const next = !locationConsentGranted;
  if (next && !confirm('允许本次行程使用精确位置？位置仅短期保存，行程结束后停止跟踪。')) return;
  const button = $('btn-location-consent');
  button.disabled = true;
  button.textContent = next ? '精确定位：授权中…' : '精确定位：关闭中…';
  try {
    let initialPosition = null;
    if (next) {
      if (!navigator.geolocation) throw new Error('当前浏览器不支持精确定位');
      setTripFeedback('正在请求浏览器定位权限…');
      initialPosition = await new Promise((resolve, reject) => {
        navigator.geolocation.getCurrentPosition(resolve, (error) => {
          reject(new Error(error.code === 1 ? '浏览器拒绝了定位权限' : '暂时无法取得当前位置'));
        }, { enableHighAccuracy: true, maximumAge: 15000, timeout: 10000 });
      });
    }
    await API.setTripConsent(activeTripId, 'precise_location', next);
    locationConsentGranted = next;
    button.textContent = '精确定位：' + (next ? '已授权' : '未授权');
    if (next) {
      const position = initialPosition;
      await API.updateTripLocation(activeTripId, {
        event_id: 'location-initial-' + Date.now(),
        location: { lng: position.coords.longitude, lat: position.coords.latitude },
        accuracy_meters: position.coords.accuracy,
        captured_at: new Date(position.timestamp).toISOString(),
      });
      startLocationTracking();
      setTripFeedback('精确定位已授权，当前位置已同步到后端。', 'success');
      toast('精确定位已开启');
    } else {
      stopLocationTracking();
      setTripFeedback('精确定位已关闭，后端不再接收位置。', 'success');
      toast('精确定位已关闭');
    }
  } catch (error) {
    if (next && locationConsentGranted) {
      try { await API.setTripConsent(activeTripId, 'precise_location', false); } catch (_) { /* 保持原错误 */ }
      locationConsentGranted = false;
    }
    button.textContent = '精确定位：' + (locationConsentGranted ? '已授权' : '未授权');
    setTripFeedback('定位操作失败：' + error.message, 'error');
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

function startLocationTracking() {
  if (!navigator.geolocation || locationWatchId != null || !activeTripId) return;
  const trackingTripId = activeTripId;
  locationWatchId = navigator.geolocation.watchPosition(async (position) => {
    if (activeTripId !== trackingTripId || !locationConsentGranted) return;
    try {
      await API.updateTripLocation(trackingTripId, {
        event_id: 'location-' + Date.now() + '-' + Math.round(position.coords.latitude * 1e5),
        location: { lng: position.coords.longitude, lat: position.coords.latitude },
        accuracy_meters: position.coords.accuracy,
        captured_at: new Date(position.timestamp).toISOString(),
      });
    } catch (error) {
      if (
        activeTripId === trackingTripId &&
        (error.status === 403 || error.status === 409)
      ) stopLocationTracking();
    }
  }, async (error) => {
    if (error.code !== 1 || activeTripId !== trackingTripId) return;
    stopLocationTracking();
    locationConsentGranted = false;
    $('btn-location-consent').textContent = '精确定位：未授权';
    setTripFeedback('浏览器已撤销定位权限，实时位置同步已停止。', 'error');
    try { await API.setTripConsent(trackingTripId, 'precise_location', false); } catch (_) { /* 已停止本地跟踪 */ }
  }, { enableHighAccuracy: true, maximumAge: 15000, timeout: 10000 });
}

function stopLocationTracking() {
  if (locationWatchId != null && navigator.geolocation) {
    navigator.geolocation.clearWatch(locationWatchId);
    locationWatchId = null;
  }
}

function travelModeName() {
  return {
    drive: '驾车',
    ride: '骑行',
    transit: '公共交通',
    walk: '步行',
  }[S.planTravelMode] || '步行';
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
      const leg = await routeLeg(prev, s.loc, S.planTravelMode, planningCity());
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
    btn.textContent = '快速路线';
  }
}

/* 真实路网距离矩阵(并发成对请求,对称近似) */
async function buildRealMatrix(points, tmode) {
  const n = points.length;
  const D = Array.from({ length: n }, () => new Array(n).fill(0));
  const pairs = [];
  for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) pairs.push([i, j]);
  await Promise.all(pairs.map(async ([i, j]) => {
    const leg = await routeLeg(points[i], points[j], tmode, planningCity());
    D[i][j] = D[j][i] = leg.distance;
  }));
  return D;
}

function legTimeEst(leg, tmode) {
  if (leg.time != null) return leg.time;
  const speed = tmode === 'drive' ? 8.3 : tmode === 'ride' ? 4 : tmode === 'transit' ? 6 : 1.2; // m/s
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
    const leg = await routeLeg(prev, s.loc, S.planTravelMode, planningCity());
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

export async function loadPlanOverview() {
  const hint = $('plan-overview-hint');
  const stats = $('plan-overview-stats');
  const list = $('plan-recent-list');
  stats.innerHTML = '';
  list.innerHTML = '';
  if (!API.user) {
    hint.textContent = API.offline ? '后端未启动' : '登录后同步正式计划';
    return;
  }
  hint.textContent = '同步中…';
  try {
    const overview = await API.getPlanOverview(4);
    const rate = overview.success_rate == null ? '—' : Math.round(overview.success_rate * 100) + '%';
    stats.innerHTML =
      '<div><b>' + overview.formal_plans + '</b><span>正式计划</span></div>' +
      '<div><b>' + rate + '</b><span>规划成功率</span></div>' +
      '<div><b>' + overview.active_trips + '</b><span>进行中行程</span></div>';
    hint.textContent = overview.recent.length ? '点选可继续' : '还没有正式 AI 计划';
    overview.recent.forEach((item) => {
      const summary = item.summary || {};
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'plan-recent';
      const title = String(item.input_text || '未命名计划').replace(/\s+/g, ' ').trim();
      const meta = (summary.stop_count || 0) + ' 站 · ' +
        fmtDist(summary.total_distance_meters || 0) +
        (item.trip_state ? ' · ' + item.trip_state : '');
      button.innerHTML =
        '<b>' + escapeHtml(title) + '</b>' +
        '<span class="recent-version">v' + item.plan_version + '</span>' +
        '<small>' + escapeHtml(meta) + '</small>' +
        '<small>' + escapeHtml(String(item.created_at || '').slice(0, 16).replace('T', ' ')) + '</small>';
      button.addEventListener('click', async () => {
        const snapshot = { ...(item.snapshot || {}) };
        snapshot.planning_run_id = item.planning_run_id;
        snapshot.plan_version = item.plan_version;
        switchActiveTrip(item.trip_id || null);
        $('plan-input').value = item.input_text || '';
        persistPlanDraft();
        const origin = snapshot.origin || S.myPos || S.map.getCenter();
        try {
          await renderAIResult(snapshot, origin, item.input_text || '', $('plan-result'));
          if (item.trip_state) {
            $('trip-state-badge').textContent = item.trip_state;
            setTripControls(item.trip_state);
          }
          if (item.trip_id) await hydrateTripRuntime();
          toast('已继续正式计划 v' + item.plan_version);
        } catch (error) {
          toast('恢复计划失败：' + error.message);
        }
      });
      list.appendChild(button);
    });
  } catch (error) {
    hint.textContent = error.message;
  }
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
        S.planTravelModeExplicit = true;
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
