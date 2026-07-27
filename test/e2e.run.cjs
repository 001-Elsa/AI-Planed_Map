/* ===================================================================
 * 随行地图 MapGo — 前端 E2E 冒烟测试(Playwright)
 *
 *   npm run test:e2e
 *
 * 需要 devDependency:playwright(npm i -D playwright && npx playwright install chromium)。
 * 策略:spawn 真实服务(临时数据目录),无头浏览器走关键用户路径。
 * 不依赖真实高德 Key —— 只验证到"Key 配置弹窗"为止的全部应用逻辑,
 * 以及模块加载零报错、admin/share 页面可用。
 * =================================================================== */
'use strict';

const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

let chromium;
try {
  ({ chromium } = require('playwright'));
} catch (e) {
  console.error('未安装 playwright(devDependency)。运行:npm i -D playwright && npx playwright install chromium');
  process.exit(1);
}

const PORT = 3800 + Math.floor(Math.random() * 100);
const BASE = `http://127.0.0.1:${PORT}`;

let passed = 0, failed = 0;
function check(name, cond, extra) {
  if (cond) { passed++; console.log('  ✓ ' + name); }
  else { failed++; console.error('  ✗ ' + name + (extra ? ' — ' + extra : '')); }
}

(async () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'mapgo-e2e-'));
  const proc = spawn(process.execPath, [path.join(__dirname, '..', 'server.js')], {
    env: Object.assign({}, process.env, { PORT: String(PORT), DATA_DIR: tmpDir }),
    stdio: 'ignore',
  });
  for (let i = 0; i < 50; i++) {
    try { const r = await fetch(BASE + '/api/health'); if (r.ok) break; } catch (e) { /* wait */ }
    await new Promise((r) => setTimeout(r, 100));
  }

  const browser = await chromium.launch();
  const errors = [];
  const newPage = async () => {
    const page = await browser.newPage({ viewport: { width: 420, height: 820 } });
    page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
    page.on('console', (m) => {
      const t = m.text();
      if (m.type() === 'error' && !t.includes('Failed to load resource')) errors.push('console: ' + t);
    });
    return page;
  };

  try {
    /* ---- 1. 主应用:模块加载 + 注册全流程 ---- */
    console.log('▶ 主应用');
    const page = await newPage();
    await page.goto(BASE, { waitUntil: 'networkidle' });
    await page.waitForTimeout(400);
    check('ES Modules 加载零报错', errors.length === 0, errors[0]);
    check('登录视图展示', await page.locator('#auth-view').isVisible());

    await page.click('#auth-tab-reg');
    await page.fill('#auth-username', 'e2euser');
    await page.fill('#auth-password', 'e2epass1');
    await page.fill('#auth-password2', 'e2epass1');
    await page.click('#auth-submit');
    await page.waitForTimeout(1200);
    check('注册后进入应用(Key 弹窗出现)', await page.locator('#setup-mask').isVisible());
    check('用户按钮显示昵称首字', (await page.locator('#btn-user').textContent()).trim() === 'e');
    check('16 个模式标签渲染', (await page.locator('#tabbar .tab').count()) === 16);

    /* 复访:token 直接进入 */
    await page.goto(BASE, { waitUntil: 'networkidle' });
    await page.waitForTimeout(600);
    check('复访凭 token 免登录', !(await page.locator('#auth-view').isVisible()));

    /* ---- 2. 管理后台 ---- */
    console.log('▶ 管理后台');
    const admin = await newPage();
    await admin.goto(BASE + '/admin.html', { waitUntil: 'domcontentloaded' });
    await admin.waitForTimeout(600);
    if (!(await admin.locator('#panel').isVisible())) {
      await admin.fill('#u', 'e2euser');
      await admin.fill('#p', 'e2epass1');
      await admin.click('button:has-text("登录")');
      await admin.waitForTimeout(800);
    }
    check('管理员(首个注册)进入后台', await admin.locator('#panel').isVisible());
    check('总览统计渲染', /用户/.test(await admin.locator('#tiles').textContent()));

    /* 配置服务端占位 Key → /api/config 生效 */
    await admin.fill('#ak', 'E2E_PLACEHOLDER_KEY');
    await admin.fill('#aj', 'E2E_PLACEHOLDER_JSCODE');
    await admin.click('button:has-text("保存")');
    await admin.waitForTimeout(500);
    const cfg = await fetch(BASE + '/api/config').then((r) => r.json());
    check('服务端 Key 代理配置生效', cfg.data.proxy === true && cfg.data.amapKey === 'E2E_PLACEHOLDER_KEY');

    /* ---- 3. 分享页 ---- */
    console.log('▶ 分享页');
    const login = await fetch(BASE + '/api/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'e2euser', password: 'e2epass1' }),
    }).then((r) => r.json());
    const share = await fetch(BASE + '/api/shares', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + login.data.token },
      body: JSON.stringify({ type: 'track', payload: { kind: 'run', name: 'e2e晨跑', distance: 4200, duration: 1500, path: [[116.4, 39.9, 0], [116.42, 39.92, 1500]] } }),
    }).then((r) => r.json());
    const sp = await newPage();
    await sp.goto(BASE + '/share.html?t=' + share.data.token, { waitUntil: 'domcontentloaded' });
    await sp.waitForTimeout(1200);
    const card = await sp.locator('#card').textContent();
    check('分享页展示轨迹信息卡', card.includes('e2e晨跑') && card.includes('4.2'));

    /* ---- 4. 游客模式与退出 ---- */
    console.log('▶ 游客与退出');
    const g = await browser.newContext();
    const gp = await g.newPage();
    await gp.goto(BASE, { waitUntil: 'networkidle' });
    await gp.waitForTimeout(400);
    await gp.click('#auth-guest');
    await gp.waitForTimeout(800);
    check('游客模式可进入(见 Key 弹窗)', await gp.locator('#setup-mask').isVisible());
    await g.close();

    check('全程无未捕获 JS 错误', errors.length === 0, errors.slice(0, 3).join(' | '));
  } finally {
    await browser.close();
    proc.kill('SIGTERM');
    try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch (e) { /* noop */ }
  }

  console.log(`\nE2E 结果: ${passed} 通过, ${failed} 失败`);
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
