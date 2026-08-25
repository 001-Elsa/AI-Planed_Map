/* 认证 UI:登录/注册视图、游客模式、用户按钮、应用启动 */
'use strict';

import { S } from '../state.js';
import { $, toast } from './dom.js';
import { store } from '../services/store.js';
import { API } from '../services/api.js?v=36';
import { loadAMap } from '../services/amap.js?v=36';
import { initMap, refreshModeData, locateCurrentPosition } from '../modes/registry.js?v=36';

let authMode = 'login';
let authAccountType = 'user';
let localAmapConfigPromise = null;
let backendRecoveryPromise = null;

export function showAuth() { $('auth-view').classList.remove('hidden'); }
export function hideAuth() { $('auth-view').classList.add('hidden'); }

function authErr(msg) {
  const e = $('auth-err');
  e.textContent = msg;
  e.classList.toggle('hidden', !msg);
}

function recoverBackend() {
  if (!API.offline) return Promise.resolve(true);
  if (backendRecoveryPromise) return backendRecoveryPromise;
  backendRecoveryPromise = API.probe()
    .then(() => {
      $('auth-offline').classList.add('hidden');
      toast('后端已连接,现在可以登录使用账号功能', 2600);
      refreshUserUI();
      return true;
    })
    .catch(() => false)
    .finally(() => { backendRecoveryPromise = null; });
  return backendRecoveryPromise;
}

export function requireLogin() {
  if (API.user) return true;
  showAuth();
  if (API.offline) {
    toast('正在重新连接后端,连接后即可登录', 2600);
    void recoverBackend();
  } else {
    toast('请先登录', 2200);
  }
  return false;
}

export function refreshUserUI() {
  $('btn-user').textContent = API.user ? API.user.nickname.slice(0, 1) : '👤';
  $('btn-user').title = API.user ? API.user.nickname : '登录 / 注册';
  refreshModeData();
}

function refreshAccountTypeUI() {
  const isAdmin = authAccountType === 'admin';
  $('auth-role-user').classList.toggle('active', !isAdmin);
  $('auth-role-user').classList.remove('admin-active');
  $('auth-role-admin').classList.toggle('active', isAdmin);
  $('auth-role-admin').classList.toggle('admin-active', isAdmin);
  $('auth-admin-token').classList.toggle('hidden', !isAdmin);
  $('auth-role-note').textContent = isAdmin
    ? '管理员注册和登录需要管理员令牌'
    : '普通用户可直接注册和登录,无需令牌';
  $('auth-submit').textContent = authMode === 'login'
    ? (isAdmin ? '管理员登录' : '用户登录')
    : (isAdmin ? '注册管理员' : '注册用户');
}

/* ---- 启动流程:登录 → 地图 ---- */
export function boot() {
  bindAuthUI();
  API.config().catch(() => {});
  window.setInterval(() => {
    if (API.offline) void recoverBackend();
  }, 10000);

  const proceed = () => { hideAuth(); startApp(); };

  if (API.token) {
    API.me().then((u) => { API.user = u; proceed(); })
      .catch(() => {
        if (API.offline) { proceed(); toast('后端未启动,进入离线游客模式', 3000); }
        else { API.saveToken(''); showAuth(); }
      });
  } else if (store.get('mapgo_guest') === '1') {
    API.req('GET', '/me').catch(() => {});
    proceed();
  } else {
    showAuth();
    API.req('GET', '/me').catch(() => {
      if (API.offline) $('auth-offline').classList.remove('hidden');
    });
  }
}

function bindAuthUI() {
  const setMode = (m) => {
    authMode = m;
    $('auth-tab-login').classList.toggle('active', m === 'login');
    $('auth-tab-reg').classList.toggle('active', m === 'reg');
    $('auth-nickname').classList.toggle('hidden', m === 'login');
    $('auth-password2').classList.toggle('hidden', m === 'login');
    refreshAccountTypeUI();
    authErr('');
  };
  $('auth-tab-login').addEventListener('click', () => setMode('login'));
  $('auth-tab-reg').addEventListener('click', () => setMode('reg'));
  $('auth-role-user').addEventListener('click', () => {
    authAccountType = 'user';
    $('auth-admin-token').value = '';
    refreshAccountTypeUI();
    authErr('');
  });
  $('auth-role-admin').addEventListener('click', () => {
    authAccountType = 'admin';
    refreshAccountTypeUI();
    authErr('');
  });
  refreshAccountTypeUI();

  $('auth-submit').addEventListener('click', async () => {
    const username = $('auth-username').value.trim();
    const password = $('auth-password').value;
    if (!username || !password) { authErr('请输入用户名和密码'); return; }
    if (authMode === 'reg') {
      if (password.length < 8) { authErr('密码至少 8 位'); return; }
      if (password !== $('auth-password2').value) { authErr('两次密码不一致'); return; }
    }
    const adminInitToken = $('auth-admin-token').value.trim();
    if (authAccountType === 'admin' && !adminInitToken) {
      authErr('管理员登录或注册需要填写管理员令牌');
      return;
    }
    const submitMode = authMode;
    const submitAccountType = authAccountType;
    const submitButton = $('auth-submit');
    submitButton.disabled = true;
    submitButton.textContent = submitMode === 'login' ? '登录中…' : '注册中…';
    try {
      let r;
      for (let attempt = 0; attempt < 2; attempt += 1) {
        try {
          r = submitMode === 'login'
            ? await API.login(username, password, submitAccountType, adminInitToken)
            : await API.register(
              username,
              password,
              $('auth-nickname').value.trim(),
              submitAccountType,
              adminInitToken,
            );
          break;
        } catch (error) {
          if (error.code !== 'AUTH_DATABASE_BUSY' || attempt === 1) throw error;
          await new Promise((resolve) => setTimeout(resolve, 350));
        }
      }
      API.saveToken(r.token);
      API.user = r.user;
      store.set('mapgo_guest', '0');
      toast((submitMode === 'login' ? '欢迎回来,' : '注册成功,') + r.user.nickname);
      hideAuth();
      if (S.map) {
        refreshUserUI();
        void locateCurrentPosition(false);
      } else await startApp();
    } catch (e) {
      const message = e.code === 'USERNAME_EXISTS'
        ? '该用户名已经注册，请切换到登录'
        : e.code === 'ADMIN_ACCOUNT_REQUIRED'
          ? '该账号不是管理员账号,请切换为普通用户登录'
          : e.code === 'ADMIN_LOGIN_REQUIRED'
            ? '这是管理员账号,请切换为管理员登录'
            : e.code === 'ADMIN_INIT_INVALID'
              ? '管理员令牌不正确'
              : e.message;
      authErr(API.offline ? '后端服务不可用,请稍后重试' : message);
      if (API.offline) $('auth-offline').classList.remove('hidden');
    } finally {
      submitButton.disabled = false;
      refreshAccountTypeUI();
    }
  });

  ['auth-username', 'auth-nickname', 'auth-password', 'auth-password2', 'auth-admin-token']
    .forEach((id) => $(id).addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      $('auth-submit').click();
    }));

  $('auth-guest').addEventListener('click', () => {
    store.set('mapgo_guest', '1');
    hideAuth();
    if (!S.map) startApp();
  });

  $('btn-user').addEventListener('click', async () => {
    if (API.user) {
      if (confirm('当前登录:' + API.user.nickname + '(' + API.user.username + ')\n要退出登录吗?')) {
        try { await API.logout(); } catch (e) { /* noop */ }
        API.saveToken('');
        API.user = null;
        toast('已退出登录');
        refreshUserUI();
      }
    } else {
      showAuth();
    }
  });
}

function getLocalAmapConfig() {
  if (!localAmapConfigPromise) {
    localAmapConfigPromise = import('/js/local-config.js')
      .then((m) => ({
        key: String(m.LOCAL_AMAP_KEY || '').trim(),
        jscode: String(m.LOCAL_AMAP_JSCODE || '').trim(),
      }))
      .catch(() => null);
  }
  return localAmapConfigPromise;
}

/* Key 来源优先级:服务端托管(代理模式)> 本机 localStorage > 本地默认凭据 */
async function startApp() {
  refreshUserUI();
  let cfg = null;
  try { cfg = await API.config(); } catch (e) { /* 离线 */ }
  const onerror = () => { toast('地图加载失败,请检查网络', 4000); };
  if (cfg && cfg.amapKey && cfg.proxy) { loadAMap(cfg.amapKey, null, true, initMap, onerror); return; }
  const key = store.get('amap_key');
  const jscode = store.get('amap_jscode');
  if (key && jscode) { loadAMap(key, jscode, false, initMap, onerror); return; }
  const localCfg = await getLocalAmapConfig();
  if (localCfg && localCfg.key && localCfg.jscode) {
    loadAMap(localCfg.key, localCfg.jscode, false, initMap, onerror);
    return;
  }
  toast('地图服务暂不可用', 4000);
}
