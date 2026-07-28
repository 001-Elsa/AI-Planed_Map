/* 后端 API 封装:token 管理、离线探测、REST 方法 */
'use strict';

import { store } from './store.js';

export const API = {
  token: store.get('mapgo_token') || '',
  user: null,        // 登录用户 {id, username, nickname, is_admin}
  offline: false,    // 后端不可用(纯静态托管时降级)

  saveToken(t) { API.token = t || ''; if (t) store.set('mapgo_token', t); else store.del('mapgo_token'); },

  async req(method, url, body) {
    const headers = { 'Content-Type': 'application/json' };
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
      const err = new Error((data && data.msg) || ('请求失败 ' + res.status));
      err.status = res.status;
      throw err;
    }
    return data.data;
  },

  register(username, password, nickname, adminInitToken) { return API.req('POST', '/register', { username, password, nickname, adminInitToken }); },
  login(username, password) { return API.req('POST', '/login', { username, password }); },
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
    const headers = { 'Content-Type': 'application/json' };
    if (API.token) headers.Authorization = 'Bearer ' + API.token;
    if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey;
    return fetch('/api/ai/plans', {
      method: 'POST',
      headers,
      body: JSON.stringify(request),
    }).then(async (res) => {
      const data = await res.json().catch(() => null);
      if (!res.ok || !data || data.ok === false) {
        const err = new Error((data && data.msg) || ('请求失败 ' + res.status));
        err.status = res.status;
        throw err;
      }
      return data.data;
    });
  },
  startPlanningConversation(request) {
    return API.req('POST', '/ai/conversations', request);
  },
  continuePlanningConversation(conversationId, baseRevision, answers) {
    return API.req('PATCH', '/ai/conversations/' + conversationId, {
      base_revision: baseRevision, answers,
    });
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
