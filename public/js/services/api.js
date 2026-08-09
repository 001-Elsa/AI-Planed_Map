/* 后端 API 封装:token 管理、离线探测、REST 方法 */
'use strict';

import { store } from './store.js';

function normalizePlanningRequest(request) {
  const normalized = { ...request };
  if (request && request.origin) {
    normalized.origin = {
      lng: Number(request.origin.lng),
      lat: Number(request.origin.lat),
    };
  }
  return normalized;
}

function validationField(details) {
  const first = details && Array.isArray(details.errors) ? details.errors[0] : null;
  if (!first || !Array.isArray(first.loc)) return '';
  return first.loc.filter((part) => part !== 'body').join('.');
}

export const API = {
  token: store.get('mapgo_token') || '',
  user: null,        // 登录用户 {id, username, nickname, is_admin}
  offline: false,    // 后端不可用(纯静态托管时降级)

  saveToken(t) { API.token = t || ''; if (t) store.set('mapgo_token', t); else store.del('mapgo_token'); },

  deviceName() {
    const platform = navigator.userAgentData && navigator.userAgentData.platform || navigator.platform;
    return String(platform || 'Web').slice(0, 80) + ' · Browser';
  },

  deviceId() {
    let id = store.get('mapgo_device_id');
    if (id) return id;
    id = globalThis.crypto && crypto.randomUUID
      ? crypto.randomUUID()
      : 'web-' + Date.now() + '-' + Math.random().toString(36).slice(2);
    store.set('mapgo_device_id', id);
    return id;
  },

  async req(method, url, body, extraHeaders) {
    const headers = Object.assign({ 'Content-Type': 'application/json' }, extraHeaders || {});
    if (API.token) headers.Authorization = 'Bearer ' + API.token;
    let res;
    try {
      res = await fetch('/api' + url, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (e) {
      API.offline = true;
      throw new Error('后端服务不可用');
    }
    API.offline = false;
    let data = null;
    try { data = await res.json(); } catch (e) { /* 非 JSON */ }
    if (!res.ok || !data || data.ok === false) {
      const field = data && data.code === 'VALIDATION_ERROR' ? validationField(data.details) : '';
      const err = new Error(
        ((data && data.msg) || ('请求失败 ' + res.status)) + (field ? '（字段：' + field + '）' : ''),
      );
      err.status = res.status;
      err.code = data && data.code;
      err.details = data && data.details;
      throw err;
    }
    return data.data;
  },

  probe() { return API.req('GET', '/health'); },

  register(username, password, nickname, accountType, adminInitToken) {
    return API.req(
      'POST',
      '/register',
      { username, password, nickname, accountType, adminInitToken },
      { 'X-Device-Name': API.deviceName(), 'X-Device-Id': API.deviceId() },
    );
  },
  login(username, password, accountType, adminInitToken) {
    return API.req(
      'POST',
      '/login',
      { username, password, accountType, adminInitToken },
      { 'X-Device-Name': API.deviceName(), 'X-Device-Id': API.deviceId() },
    );
  },
  logout() { return API.req('POST', '/logout'); },
  me() { return API.req('GET', '/me'); },
  config() { return API.req('GET', '/config'); },

  listFavorites() { return API.req('GET', '/favorites?limit=200'); },
  addFavorite(fav) { return API.req('POST', '/favorites', fav); },
  delFavorite(id) { return API.req('DELETE', '/favorites/' + id); },

  listPlans() { return API.req('GET', '/plans?limit=100'); },
  addPlan(name, data) { return API.req('POST', '/plans', { name, data }); },
  delPlan(id) { return API.req('DELETE', '/plans/' + id); },
  aiPlan(request, idempotencyKey) {
    const headers = idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined;
    return API.req('POST', '/ai/plans', request, headers);
  },
  async startPlanningConversation(request) {
    const normalizedRequest = normalizePlanningRequest(request);
    const serialized = JSON.stringify(normalizedRequest);
    let pending = null;
    try { pending = JSON.parse(store.get('mapgo_ai_pending_request') || 'null'); } catch (_) { /* noop */ }
    const idempotencyKey = pending && pending.request === serialized
      ? pending.key
      : (globalThis.crypto && crypto.randomUUID ? crypto.randomUUID() : 'web-' + Date.now() + '-' + Math.random());
    store.set('mapgo_ai_pending_request', JSON.stringify({ request: serialized, key: idempotencyKey }));
    try {
      const data = await API.req(
        'POST',
        '/ai/conversations',
        normalizedRequest,
        { 'Idempotency-Key': idempotencyKey },
      );
      store.del('mapgo_ai_pending_request');
      return data;
    } catch (error) {
      const definitiveClientFailure = error.status >= 400 && error.status < 500 &&
        error.code !== 'REQUEST_IN_PROGRESS';
      const failedServerExecution = error.status >= 500 || error.code === 'PREVIOUS_REQUEST_FAILED';
      if (definitiveClientFailure || failedServerExecution) {
        store.del('mapgo_ai_pending_request');
      }
      throw error;
    }
  },
  continuePlanningConversation(conversationId, baseRevision, answers) {
    return API.req('PATCH', '/ai/conversations/' + conversationId, {
      base_revision: baseRevision, answers,
    });
  },
  planningCapabilities() { return API.req('GET', '/ai/capabilities'); },
  getPlanOverview(limit) {
    return API.req('GET', '/ai/plans/overview?limit=' + (limit || 5));
  },
  decidePlanPatch(planningRunId, patchId, accept) {
    return API.req('POST', '/ai/plans/' + planningRunId + '/patches/' + patchId + '/decision', { accept });
  },
  createTrip(planningRunId) { return API.req('POST', '/companion/trips', { planning_run_id: planningRunId }); },
  getTrip(tripId) { return API.req('GET', '/companion/trips/' + tripId); },
  transitionTrip(tripId, targetState, reason) {
    return API.req('POST', '/companion/trips/' + tripId + '/transition', {
      target_state: targetState, reason,
    });
  },
  setTripConsent(tripId, scope, granted) {
    return API.req('POST', '/companion/trips/' + tripId + '/consents', { scope, granted });
  },
  updateTripLocation(tripId, payload) {
    return API.req('POST', '/companion/trips/' + tripId + '/location', payload);
  },
  sendTripEvent(tripId, event) {
    return API.req('POST', '/companion/trips/' + tripId + '/events', event);
  },
  replanTrip(tripId, payload) {
    return API.req('POST', '/companion/trips/' + tripId + '/replan', payload);
  },
  getTripSummary(tripId) { return API.req('GET', '/companion/trips/' + tripId + '/summary'); },
  deleteTripLocations(tripId) { return API.req('DELETE', '/companion/trips/' + tripId + '/locations'); },

  listTracks(kind, limit) {
    const q = ['limit=' + (limit || 100)];
    if (kind) q.push('kind=' + kind);
    return API.req('GET', '/tracks?' + q.join('&'));
  },
  addTrack(t) { return API.req('POST', '/tracks', t); },
  delTrack(id) { return API.req('DELETE', '/tracks/' + id); },

  listCheckins() { return API.req('GET', '/checkins?limit=200'); },
  addCheckin(c) { return API.req('POST', '/checkins', c); },
  delCheckin(id) { return API.req('DELETE', '/checkins/' + id); },

  stats() { return API.req('GET', '/stats'); },

  createShare(type, payload) { return API.req('POST', '/shares', { type, payload }); },
  getShare(token) { return API.req('GET', '/share/' + token); },

  listFriends() { return API.req('GET', '/friends'); },
  requestFriend(username) { return API.req('POST', '/friends/request', { username }); },
  respondFriend(id, accept) { return API.req('POST', '/friends/respond', { id, accept }); },
  delFriend(id) { return API.req('DELETE', '/friends/' + id); },
  friendFavorites(uid) { return API.req('GET', '/friends/' + uid + '/favorites'); },
  leaderboard(days) { return API.req('GET', '/leaderboard?days=' + (days || 7)); },
};
