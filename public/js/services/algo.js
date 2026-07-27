/* 路径算法(纯函数,与地图/DOM 无关,可独立测试)
 * 开放路径 TSP:起点固定为 0 号(当前位置),访问全部站点,不要求返回 */
'use strict';

import { haversine } from './format.js';

/* 精确解:全排列枚举。D 为 (n+1)x(n+1) 距离矩阵(0 = 起点),n ≤ 6 时毫秒级 */
export function solveOrderExact(D, n) {
  const idx = Array.from({ length: n }, (_, i) => i + 1);
  let best = null, bestLen = Infinity;
  const permute = (arr, cur) => {
    if (!arr.length) {
      let len = 0, prev = 0;
      for (const x of cur) { len += D[prev][x]; prev = x; }
      if (len < bestLen) { bestLen = len; best = cur.slice(); }
      return;
    }
    for (let i = 0; i < arr.length; i++) {
      cur.push(arr[i]);
      permute(arr.slice(0, i).concat(arr.slice(i + 1)), cur);
      cur.pop();
    }
  };
  permute(idx, []);
  return (best || idx).map((x) => x - 1);
}

/* 近似解:最近邻构造 + 2-opt 迭代(站点多时用,球面距离) */
export function solveOrder(origin, points) {
  const n = points.length;
  if (n <= 1) return points.map((_, i) => i);
  const all = [origin].concat(points);
  const D = [];
  for (let i = 0; i < all.length; i++) {
    D.push([]);
    for (let j = 0; j < all.length; j++) D[i].push(i === j ? 0 : haversine(all[i], all[j]));
  }
  const visited = new Array(n + 1).fill(false);
  visited[0] = true;
  let cur = 0;
  const route = [];
  for (let k = 0; k < n; k++) {
    let best = -1, bd = Infinity;
    for (let j = 1; j <= n; j++) if (!visited[j] && D[cur][j] < bd) { bd = D[cur][j]; best = j; }
    visited[best] = true;
    route.push(best);
    cur = best;
  }
  const pathLen = (r) => {
    let s = D[0][r[0]];
    for (let i = 0; i < r.length - 1; i++) s += D[r[i]][r[i + 1]];
    return s;
  };
  let improved = true, guard = 0;
  while (improved && guard++ < 200) {
    improved = false;
    for (let i = 0; i < route.length - 1; i++) {
      for (let j = i + 1; j < route.length; j++) {
        const cand = route.slice(0, i).concat(route.slice(i, j + 1).reverse(), route.slice(j + 1));
        if (pathLen(cand) + 1e-9 < pathLen(route)) {
          route.splice(0, route.length, ...cand);
          improved = true;
        }
      }
    }
  }
  return route.map((x) => x - 1);
}

/* 几何中位数(Weiszfeld 迭代):对所有人总距离最短的点 */
export function geometricMedian(people, iterations = 40) {
  let cx = people.reduce((s, p) => s + p.lng, 0) / people.length;
  let cy = people.reduce((s, p) => s + p.lat, 0) / people.length;
  for (let it = 0; it < iterations; it++) {
    let sw = 0, sx = 0, sy = 0;
    for (const p of people) {
      const d = Math.max(1e-9, haversine({ lng: cx, lat: cy }, p));
      const w = 1 / d;
      sw += w; sx += w * p.lng; sy += w * p.lat;
    }
    cx = sx / sw; cy = sy / sw;
  }
  return { lng: cx, lat: cy };
}
