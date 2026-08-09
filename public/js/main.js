/* 应用入口:启动认证流程,注册 Service Worker */
'use strict';

import { boot } from './ui/auth.js?v=33';

if ('serviceWorker' in navigator && location.protocol !== 'file:') {
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (sessionStorage.getItem('mapgo_sw_reloaded') === '1') return;
    sessionStorage.setItem('mapgo_sw_reloaded', '1');
    location.reload();
  });
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('sw.js').then(() => {
      sessionStorage.removeItem('mapgo_sw_reloaded');
    }).catch(() => {});
  });
}

boot();
