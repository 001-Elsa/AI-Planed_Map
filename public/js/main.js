/* 应用入口:启动认证流程,注册 Service Worker */
'use strict';

import { boot } from './ui/auth.js';

if ('serviceWorker' in navigator && location.protocol !== 'file:') {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('sw.js').catch(() => {});
  });
}

boot();
