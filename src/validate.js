'use strict';

function isFiniteNumber(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

function validLngLat(lng, lat) {
  return isFiniteNumber(lng) && isFiniteNumber(lat) &&
    lng >= -180 && lng <= 180 && lat >= -90 && lat <= 90;
}

function cleanText(value, maxLen) {
  if (typeof value !== 'string') return '';
  return value.trim().slice(0, maxLen);
}

function jsonWithin(value, maxBytes) {
  const json = JSON.stringify(value);
  return Buffer.byteLength(json, 'utf8') <= maxBytes ? json : null;
}

function cleanPath(points, maxPoints = 10000) {
  if (!Array.isArray(points) || points.length < 2 || points.length > maxPoints) return null;
  const out = [];
  for (const p of points) {
    if (!Array.isArray(p) || p.length < 2) return null;
    const lng = Number(p[0]);
    const lat = Number(p[1]);
    if (!validLngLat(lng, lat)) return null;
    if (p.length >= 3 && p[2] != null) {
      const t = Number(p[2]);
      if (!Number.isFinite(t) || t < 0) return null;
      out.push([lng, lat, t]);
    } else {
      out.push([lng, lat]);
    }
  }
  return out;
}

function validDistance(v) {
  return isFiniteNumber(v) && v >= 0 && v <= 200000000;
}

function validDuration(v) {
  return v == null || (isFiniteNumber(Number(v)) && Number(v) >= 0 && Number(v) <= 366 * 24 * 3600);
}

module.exports = {
  isFiniteNumber,
  validLngLat,
  cleanText,
  jsonWithin,
  cleanPath,
  validDistance,
  validDuration,
};
