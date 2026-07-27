/* DOM 基础工具:选择器、HTML 转义、toast、语音播报 */
'use strict';

import { S } from '../state.js';

export const $ = (id) => document.getElementById(id);

export function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

let toastTimer = null;
export function toast(msg, ms = 2600) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), ms);
}

export function speak(text) {
  if (!S.voiceOn || !window.speechSynthesis) return;
  try {
    const u = new SpeechSynthesisUtterance(text);
    u.lang = 'zh-CN';
    u.rate = 1.05;
    speechSynthesis.speak(u);
  } catch (e) { /* 不支持则静默 */ }
}
