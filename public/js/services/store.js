/* 本地存储封装:localStorage 不可用时(隐私模式等)降级为内存 */
'use strict';

const mem = {};

export const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return mem[k] || null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { mem[k] = v; } },
  del(k) { try { localStorage.removeItem(k); } catch (e) { delete mem[k]; } },
};
