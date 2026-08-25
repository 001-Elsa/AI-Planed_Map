/* 社交与个人数据模式:足迹打卡 / 收藏 / 好友与排行榜 / 数据统计与热力图 / 每周提醒 */
'use strict';

import { S } from '../state.js';
import { $, escapeHtml, toast } from '../ui/dom.js';
import { API } from '../services/api.js?v=36';
import { fmtDist, fmtDur, haversine } from '../services/format.js';
import { searchNearestPOI } from '../services/amap.js?v=36';
import { requireLogin } from '../ui/auth.js?v=36';
import { MODES } from './registry.js?v=36';
import { trackPath } from './route.js?v=36';

let leaderboardRefreshTimer = null;

/* ---------------- 生命周期 ---------------- */
export function clearAll() {
  clearFootMarkers();
  clearFavMarkers();
  clearFriendOverlays();
  if (S.heatOn) toggleHeatmap();
  clearTimeout(leaderboardRefreshTimer);
  leaderboardRefreshTimer = null;
}

export function bindSocialUI() {
  /* 足迹 */
  document.querySelectorAll('#foot-emoji button').forEach((b) => {
    b.addEventListener('click', () => {
      S.footEmoji = b.textContent;
      document.querySelectorAll('#foot-emoji button').forEach((x) => x.classList.toggle('active', x === b));
    });
  });
  $('btn-checkin').addEventListener('click', doCheckin);

  /* 好友 */
  $('btn-friend-add').addEventListener('click', addFriend);
  $('friend-username').addEventListener('keydown', (e) => { if (e.key === 'Enter') addFriend(); });
  document.querySelectorAll('#lb-seg .seg-btn').forEach((b) => {
    b.addEventListener('click', () => {
      document.querySelectorAll('#lb-seg .seg-btn').forEach((x) => x.classList.toggle('active', x === b));
      loadLeaderboard(parseInt(b.dataset.days, 10));
    });
  });

  /* 热力足迹 */
  $('btn-heatmap').addEventListener('click', toggleHeatmap);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible' || S.currentMode !== 'friends' || !API.user) return;
    const activeDays = document.querySelector('#lb-seg .seg-btn.active');
    loadLeaderboard(activeDays ? parseInt(activeDays.dataset.days, 10) : 1);
  });
}

/* ---------------- 足迹打卡 ---------------- */
function clearFootMarkers() {
  if (S.footMarkers.length) S.map.remove(S.footMarkers);
  S.footMarkers = [];
  if (S.footPendingMarker) { S.map.remove(S.footPendingMarker); S.footPendingMarker = null; }
  S.footPending = null;
}

export function handleFootClick(e) {
  S.footPending = { lng: e.lnglat.lng, lat: e.lnglat.lat };
  if (S.footPendingMarker) S.map.remove(S.footPendingMarker);
  S.footPendingMarker = new AMap.Marker({
    position: e.lnglat,
    content: '<div class="flagpin">' + S.footEmoji + '</div>',
    offset: new AMap.Pixel(-15, -30),
    zIndex: 170,
  });
  S.map.add(S.footPendingMarker);
  $('foot-hint').textContent = '已选位置,点「在此打卡」';
}

async function doCheckin() {
  if (!requireLogin()) return;
  const pos = S.footPending || S.myPos;
  if (!pos) { toast('先定位(⌖)或点地图选个位置'); return; }
  const note = $('foot-note').value.trim();
  let name = '我的足迹';
  try {
    const poi = await searchNearestPOI('', pos);
    if (poi) name = poi.name;
  } catch (e) { /* 命名失败用默认 */ }
  try {
    await API.addCheckin({ name, note, emoji: S.footEmoji, lng: pos.lng, lat: pos.lat });
    toast(S.footEmoji + ' 打卡成功!');
    $('foot-note').value = '';
    if (S.footPendingMarker) { S.map.remove(S.footPendingMarker); S.footPendingMarker = null; }
    S.footPending = null;
    loadCheckins();
  } catch (e) { toast(e.message); }
}

export async function loadCheckins() {
  const listEl = $('foot-list');
  listEl.innerHTML = '';
  if (S.footMarkers.length) { S.map.remove(S.footMarkers); S.footMarkers = []; }
  if (!API.user) {
    $('foot-hint').textContent = API.offline ? '后端未启动' : '登录后开始记录足迹';
    $('foot-summary').innerHTML = '';
    return;
  }
  try {
    const rows = await API.listCheckins();
    $('foot-hint').textContent = rows.length ? '共 ' + rows.length + ' 个足迹' : '在地图上留下第一个足迹吧';
    const now = new Date();
    const monthCount = rows.filter((item) => {
      const created = new Date(String(item.created_at || '').replace(' ', 'T'));
      return created.getFullYear() === now.getFullYear() && created.getMonth() === now.getMonth();
    }).length;
    const noteCount = rows.filter((item) => item.note).length;
    $('foot-summary').innerHTML = '<div><b>' + rows.length + '</b><span>永久足迹</span></div>' +
      '<div><b>' + monthCount + '</b><span>本月新增</span></div>' +
      '<div><b>' + noteCount + '</b><span>带日记</span></div>';
    rows.forEach((c) => {
      const mk = new AMap.Marker({
        position: [c.lng, c.lat],
        content: '<div class="flagpin">' + escapeHtml(c.emoji || '📍') + '</div>',
        offset: new AMap.Pixel(-15, -30),
        zIndex: 130,
      });
      mk.on('click', () => {
        S.infoWindow.setContent('<b>' + escapeHtml(c.emoji || '📍') + ' ' + escapeHtml(c.name) + '</b><br>' +
          (c.note ? escapeHtml(c.note) + '<br>' : '') +
          '<span style="color:#889">' + escapeHtml((c.created_at || '').slice(0, 16)) + '</span>');
        S.infoWindow.open(S.map, [c.lng, c.lat]);
      });
      S.footMarkers.push(mk);

      const item = document.createElement('div');
      item.className = 'poi-item';
      item.innerHTML =
        '<div><div class="name">' + escapeHtml(c.emoji || '📍') + ' ' + escapeHtml(c.name) + '</div>' +
        '<div class="addr">' + (c.note ? escapeHtml(c.note) + ' · ' : '') + escapeHtml((c.created_at || '').slice(0, 16)) + '</div></div>';
      const del = document.createElement('button');
      del.className = 'small-btn danger';
      del.textContent = '删';
      del.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        try { await API.delCheckin(c.id); loadCheckins(); } catch (e) { toast(e.message); }
      });
      item.appendChild(del);
      item.addEventListener('click', () => S.map.setZoomAndCenter(16, [c.lng, c.lat]));
      listEl.appendChild(item);
    });
    if (S.footMarkers.length) {
      S.map.add(S.footMarkers);
      S.map.setFitView(S.footMarkers, false, [70, 130, 70, 70]);
    }
  } catch (e) {
    $('foot-hint').textContent = e.message;
  }
}

/* ---------------- 收藏 ---------------- */
function clearFavMarkers() {
  if (S.favMarkers.length) S.map.remove(S.favMarkers);
  S.favMarkers = [];
}

export async function loadFavorites() {
  const listEl = $('fav-list');
  listEl.innerHTML = '';
  clearFavMarkers();
  if (!API.user) {
    $('fav-hint').textContent = API.offline ? '后端未启动' : '登录后可在各模式里点 ⭐ 收藏地点';
    $('fav-summary').innerHTML = '';
    return;
  }
  try {
    const rows = await API.listFavorites();
    $('fav-hint').textContent = rows.length ? rows.length + ' 个地点' : '还没有收藏,去各模式里点 ⭐ 吧';
    const modeCount = new Set(rows.map((item) => item.mode || 'other')).size;
    const nearby = S.myPos
      ? rows.filter((item) => haversine(S.myPos, { lng: item.lng, lat: item.lat }) <= 3000).length
      : 0;
    $('fav-summary').innerHTML = '<div><b>' + rows.length + '</b><span>云端收藏</span></div>' +
      '<div><b>' + modeCount + '</b><span>地点分类</span></div>' +
      '<div><b>' + (S.myPos ? nearby : '—') + '</b><span>3km 内</span></div>';
    rows.forEach((f) => {
      const emoji = (MODES[f.mode] && MODES[f.mode].emoji) || '⭐';
      const mk = new AMap.Marker({
        position: [f.lng, f.lat],
        content: '<div class="poi-marker" style="--mk:#eab308">' + emoji + '</div>',
        offset: new AMap.Pixel(-17, -30),
        zIndex: 120,
      });
      const showInfo = () => {
        S.infoWindow.setContent('<b>' + emoji + ' ' + escapeHtml(f.name) + '</b><br>' + escapeHtml(f.address || ''));
        S.infoWindow.open(S.map, [f.lng, f.lat]);
      };
      mk.on('click', showInfo);
      S.favMarkers.push(mk);

      const d = S.myPos ? haversine(S.myPos, { lng: f.lng, lat: f.lat }) : null;
      const item = document.createElement('div');
      item.className = 'poi-item';
      item.innerHTML =
        '<div><div class="name">' + emoji + ' ' + escapeHtml(f.name) + '</div>' +
        '<div class="addr">' + escapeHtml(f.address || '') + '</div></div>';
      const right = document.createElement('div');
      right.className = 'saved-btns';
      if (d != null) {
        const ds = document.createElement('span');
        ds.className = 'dist';
        ds.textContent = fmtDist(d);
        right.appendChild(ds);
      }
      const del = document.createElement('button');
      del.className = 'small-btn danger';
      del.textContent = '删';
      del.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        try { await API.delFavorite(f.id); loadFavorites(); } catch (e) { toast(e.message); }
      });
      right.appendChild(del);
      item.appendChild(right);
      item.addEventListener('click', () => {
        S.map.setZoomAndCenter(17, [f.lng, f.lat]);
        showInfo();
      });
      listEl.appendChild(item);
    });
    if (S.favMarkers.length) {
      S.map.add(S.favMarkers);
      S.map.setFitView(S.favMarkers, false, [70, 130, 70, 70]);
    }
  } catch (e) {
    $('fav-hint').textContent = e.message;
  }
}

/* ---------------- 好友与排行榜 ---------------- */
function clearFriendOverlays() {
  if (S.friendOverlays.length) S.map.remove(S.friendOverlays);
  S.friendOverlays = [];
}

async function addFriend() {
  if (!requireLogin()) return;
  const name = $('friend-username').value.trim();
  if (!name) { toast('输入对方用户名'); return; }
  try {
    const r = await API.requestFriend(name);
    toast('已向 ' + r.nickname + ' 发送好友请求');
    $('friend-username').value = '';
    loadFriends();
  } catch (e) { toast(e.message); }
}

export async function loadFriends() {
  const incomingEl = $('friends-incoming');
  const listEl = $('friends-list');
  incomingEl.innerHTML = '';
  listEl.innerHTML = '';
  $('leaderboard').innerHTML = '';
  if (!API.user) {
    $('friends-hint').textContent = API.offline ? '后端未启动' : '登录后添加好友、看排行榜';
    return;
  }
  try {
    const d = await API.listFriends();
    $('friends-hint').textContent = d.accepted.length + ' 位好友';

    d.incoming.forEach((f) => {
      const row = document.createElement('div');
      row.className = 'saved-item';
      row.innerHTML = '<div><b>' + escapeHtml(f.nickname) + '</b><div class="addr">请求加你为好友</div></div>';
      const btns = document.createElement('div');
      btns.className = 'saved-btns';
      const okB = document.createElement('button');
      okB.className = 'small-btn accent'; okB.textContent = '同意';
      okB.addEventListener('click', async () => { try { await API.respondFriend(f.id, true); loadFriends(); } catch (e) { toast(e.message); } });
      const noB = document.createElement('button');
      noB.className = 'small-btn'; noB.textContent = '拒绝';
      noB.addEventListener('click', async () => { try { await API.respondFriend(f.id, false); loadFriends(); } catch (e) { toast(e.message); } });
      btns.appendChild(okB); btns.appendChild(noB);
      row.appendChild(btns);
      incomingEl.appendChild(row);
    });
    d.outgoing.forEach((f) => {
      const row = document.createElement('div');
      row.className = 'saved-item';
      row.innerHTML = '<div><b>' + escapeHtml(f.nickname) + '</b><div class="addr">等待对方同意…</div></div>';
      incomingEl.appendChild(row);
    });

    d.accepted.forEach((f) => {
      const row = document.createElement('div');
      row.className = 'saved-item';
      row.innerHTML = '<div><b>' + escapeHtml(f.nickname) + '</b><div class="addr">@' + escapeHtml(f.username) + '</div></div>';
      const btns = document.createElement('div');
      btns.className = 'saved-btns';
      const favB = document.createElement('button');
      favB.className = 'small-btn'; favB.textContent = '⭐ 看收藏';
      favB.addEventListener('click', () => viewFriendFavs(f.uid, f.nickname));
      const delB = document.createElement('button');
      delB.className = 'small-btn danger'; delB.textContent = '删';
      delB.addEventListener('click', async () => {
        if (!confirm('删除好友 ' + f.nickname + '?')) return;
        try { await API.delFriend(f.id); loadFriends(); } catch (e) { toast(e.message); }
      });
      btns.appendChild(favB); btns.appendChild(delB);
      row.appendChild(btns);
      listEl.appendChild(row);
    });
    if (!d.accepted.length && !d.incoming.length && !d.outgoing.length) {
      listEl.innerHTML = '<p class="muted">还没有好友,输入对方注册的用户名来添加吧。</p>';
    }
    const activeDays = document.querySelector('#lb-seg .seg-btn.active');
    loadLeaderboard(activeDays ? parseInt(activeDays.dataset.days, 10) : 7);
  } catch (e) {
    $('friends-hint').textContent = e.message;
  }
}

async function loadLeaderboard(days) {
  const el = $('leaderboard');
  if (!API.user) return;
  el.innerHTML = '<p class="muted">加载中…</p>';
  try {
    const d = await API.leaderboard(days);
    const medals = ['🥇', '🥈', '🥉'];
    el.innerHTML = d.rows.map((r, i) =>
      '<div class="lb-row' + (r.uid === d.me ? ' me' : '') + '">' +
      '<span class="lb-rank">' + (medals[i] || (i + 1)) + '</span>' +
      '<span class="lb-name">' + escapeHtml(r.nickname) + (r.uid === d.me ? '(我)' : '') + '</span>' +
      '<span class="lb-val"><b>' + (r.distance / 1000).toFixed(1) + '</b> 公里 · ' + r.count + ' 次</span>' +
      '</div>').join('') || '<p class="muted">近' + days + '天大家都还没动 😴</p>';
    const updated = new Date(d.updatedAt);
    $('lb-updated').textContent = '自然日统计 · 更新于 ' +
      updated.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) +
      ' · 下次跨日自动刷新';
    clearTimeout(leaderboardRefreshTimer);
    const nextRefresh = new Date(d.nextDailyRefreshAt).getTime() - Date.now() + 1000;
    leaderboardRefreshTimer = setTimeout(() => loadLeaderboard(days), Math.max(1000, nextRefresh));
  } catch (e) { el.innerHTML = '<p class="err">' + escapeHtml(e.message) + '</p>'; }
}

async function viewFriendFavs(uid, nickname) {
  try {
    const d = await API.friendFavorites(uid);
    clearFriendOverlays();
    if (!d.favorites.length) { toast(nickname + ' 还没有收藏'); return; }
    d.favorites.forEach((f) => {
      const emoji = (MODES[f.mode] && MODES[f.mode].emoji) || '⭐';
      const mk = new AMap.Marker({
        position: [f.lng, f.lat],
        content: '<div class="poi-marker" style="--mk:#f472b6">' + emoji + '</div>',
        offset: new AMap.Pixel(-17, -30), zIndex: 125,
      });
      mk.on('click', () => {
        S.infoWindow.setContent('<b>' + emoji + ' ' + escapeHtml(f.name) + '</b><br>' +
          escapeHtml(f.address || '') + '<br><span style="color:#889">' + escapeHtml(nickname) + ' 的收藏</span>');
        S.infoWindow.open(S.map, [f.lng, f.lat]);
      });
      S.friendOverlays.push(mk);
    });
    S.map.add(S.friendOverlays);
    S.map.setFitView(S.friendOverlays, false, [70, 130, 70, 70]);
    toast('已在地图上显示 ' + nickname + ' 的 ' + d.favorites.length + ' 个收藏(粉色标记)', 3500);
  } catch (e) { toast(e.message); }
}

/* ---------------- 统计与热力图 ---------------- */
export async function loadStats() {
  const body = $('stats-body');
  body.innerHTML = '';
  if (!API.user) {
    $('stats-hint').textContent = API.offline ? '后端未启动' : '登录后查看你的数据';
    body.innerHTML = '<p class="muted">统计包含:跑步/骑行总里程与次数、近 8 周运动量、收藏与足迹数量。</p>';
    return;
  }
  $('stats-hint').textContent = '加载中…';
  try {
    const s = await API.stats();
    $('stats-hint').textContent = '';
    const kindMap = {};
    (s.byKind || []).forEach((k) => { kindMap[k.kind] = k; });
    const run = kindMap.run || { count: 0, distance: 0, duration: 0 };
    const ride = kindMap.ride || { count: 0, distance: 0, duration: 0 };

    let html =
      '<div class="stat-tiles">' +
      '<div class="tile"><b>' + (run.distance / 1000).toFixed(1) + '</b><span>跑步公里 · ' + run.count + ' 次</span></div>' +
      '<div class="tile"><b>' + (ride.distance / 1000).toFixed(1) + '</b><span>骑行公里 · ' + ride.count + ' 次</span></div>' +
      '<div class="tile"><b>' + s.counts.checkins + '</b><span>足迹打卡</span></div>' +
      '<div class="tile"><b>' + s.counts.favorites + '</b><span>收藏地点</span></div>' +
      '<div class="tile"><b>' + s.currentStreakDays + '</b><span>连续运动天数</span></div>' +
      '<div class="tile"><b>' + fmtDur((run.duration || 0) + (ride.duration || 0)) + '</b><span>累计运动时长</span></div>' +
      '</div>';

    const weekly = s.weekly || [];
    if (weekly.some((item) => item.distance > 0)) {
      const max = Math.max(...weekly.map((w) => w.distance), 1);
      html += '<div class="chart-title">近 8 周运动里程(公里)</div><div class="bar-chart">';
      weekly.forEach((w) => {
        const h = Math.max(6, Math.round(w.distance / max * 72));
        html += '<div class="bar-col"><span class="bar-val">' + (w.distance / 1000).toFixed(1) + '</span>' +
          '<div class="bar" style="height:' + h + 'px"></div>' +
          '<span class="bar-lab">' + escapeHtml(String(w.week).slice(5)) + '周</span></div>';
      });
      html += '</div>';
    } else {
      html += '<p class="muted">还没有运动记录,去跑步/骑行模式「开始记录」吧 🎽</p>';
    }

    if (s.recentCheckins && s.recentCheckins.length) {
      html += '<div class="chart-title">最近足迹</div>' +
        s.recentCheckins.map((c) =>
          '<div class="mini-row">' + escapeHtml(c.emoji || '📍') + ' ' + escapeHtml(c.name) +
          ' <span class="muted">' + escapeHtml((c.created_at || '').slice(5, 16)) + '</span></div>').join('');
    }
    html += '<p class="muted" style="margin-top:8px">数据保存在账号数据库中 · ' +
      escapeHtml(s.timezone || 'Asia/Shanghai') + ' 统计 · 账号创建于 ' +
      escapeHtml((s.since || '').slice(0, 10)) + ' · 计划 ' + s.counts.plans +
      ' 个 · 路线记录 ' + s.counts.tracks + ' 条</p>';
    body.innerHTML = html;
  } catch (e) {
    $('stats-hint').textContent = e.message;
  }
}

async function toggleHeatmap() {
  if (S.heatOn) {
    if (S.heatLayer) S.heatLayer.setMap(null);
    S.heatLayer = null;
    S.heatOn = false;
    $('btn-heatmap').textContent = '🔥 热力足迹';
    return;
  }
  if (!requireLogin()) return;
  try {
    const rows = await API.listTracks(null, 500);
    const pts = [];
    rows.forEach((t) => {
      trackPath(t).forEach((p, i) => { if (i % 2 === 0) pts.push({ lng: p[0], lat: p[1], count: 1 }); });
    });
    if (!pts.length) { toast('还没有轨迹数据,先去跑一跑吧'); return; }
    AMap.plugin('AMap.HeatMap', () => {
      S.heatLayer = new AMap.HeatMap(S.map, { radius: 14, opacity: [0, 0.85] });
      S.heatLayer.setDataSet({ data: pts, max: 5 });
      S.heatOn = true;
      $('btn-heatmap').textContent = '🔥 关闭热力';
      S.map.setZoomAndCenter(13, [pts[0].lng, pts[0].lat]);
      toast('热力图已叠加:' + rows.length + ' 条轨迹,越亮代表跑得越多');
    });
  } catch (e) { toast(e.message); }
}

/* ---------------- 每周运动提醒(站内) ---------------- */
export function checkNudge() {
  if (!API.user) return;
  API.listTracks().then((rows) => {
    const last = rows && rows[0];
    const stale = !last || (Date.now() - new Date(String(last.created_at).replace(' ', 'T')).getTime()) > 7 * 86400 * 1000;
    if (stale) toast('🏃 本周还没有运动记录,找时间出去动一动吧!', 5000);
  }).catch(() => {});
}
