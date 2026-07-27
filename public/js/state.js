/* 全局共享状态(单一数据源):所有模块通过 S 读写,避免隐式全局变量 */
'use strict';

export const DEFAULT_CENTER = [116.397428, 39.90923];

export const S = {
  /* 地图核心 */
  map: null,
  myPos: null,            // {lng, lat}
  myMarker: null,
  currentMode: 'normal',
  infoWindow: null,
  searchTimer: null,

  /* POI 模式 */
  poiMarkers: [],
  quickOverlays: [],      // “到这去”临时路线
  lastPois: [],
  activeChipIdx: 0,
  sortBy: 'dist',
  meetOverlays: [],       // 找中间点

  /* 跑步/骑行/公交 */
  waypoints: [],
  wpMarkers: [],
  routeLines: [],
  lastTrack: null,
  transitData: null,
  transitOverlays: [],
  rec: {                  // GPS 实况记录
    active: false, paused: false, watchId: null, timer: null,
    points: [], dist: 0, startTs: 0, pausedMs: 0, pauseTs: 0,
    line: null, kmMarks: [], nextKm: 1000,
  },
  replay: { active: false, raf: 0, marker: null, line: null },

  /* 计划 */
  planOverlays: [],
  planTravelMode: 'walk',
  lastPlan: null,         // {text, travelMode, depart, stay} 用于保存
  lastPlanCtx: null,      // {origin, stops, legs, missed} 用于调序/分享

  /* 足迹 / 收藏 / 好友 / 统计 */
  footEmoji: '📍',
  footPending: null,
  footPendingMarker: null,
  footMarkers: [],
  favMarkers: [],
  friendOverlays: [],
  heatLayer: null,
  heatOn: false,

  /* 偏好 */
  darkMode: false,
  voiceOn: true,
};
