import assert from 'node:assert/strict';
import test from 'node:test';

const { API } = await import('../public/js/services/api.js?recovery-test');

test('后端恢复后探测请求会自动解除离线状态', async () => {
  globalThis.fetch = async () => { throw new TypeError('connection refused'); };
  await assert.rejects(() => API.probe());
  assert.equal(API.offline, true);

  globalThis.fetch = async (url) => {
    assert.equal(String(url), '/api/health');
    return new Response(JSON.stringify({
      ok: true,
      data: { status: 'ok', databaseConnected: true },
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  const health = await API.probe();
  assert.equal(health.databaseConnected, true);
  assert.equal(API.offline, false);
});

test('AI 规划请求只发送经纬度而不泄漏缓存位置的名称字段', async () => {
  let submitted;
  globalThis.fetch = async (url, options) => {
    assert.equal(String(url), '/api/ai/conversations');
    submitted = JSON.parse(options.body);
    return new Response(JSON.stringify({
      ok: true,
      data: { conversation_id: 'conversation-1', status: 'need_clarification' },
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  await API.startPlanningConversation({
    text: '花园酒店 友谊商店 盒马鲜生 万国广场',
    origin: { lng: 113.3, lat: 23.135, name: '缓存的完整地址' },
    transport_mode: 'walking',
  });

  assert.deepEqual(submitted.origin, { lng: 113.3, lat: 23.135 });
});

test('服务端规划失败后下一次重试使用新的幂等键', async () => {
  const keys = [];
  let attempt = 0;
  globalThis.fetch = async (_url, options) => {
    keys.push(options.headers['Idempotency-Key']);
    attempt += 1;
    const payload = attempt === 1
      ? { ok: false, code: 'UPSTREAM_SERVICE_ERROR', msg: '地图服务暂时不可用' }
      : { ok: true, data: { conversation_id: 'conversation-2', status: 'success' } };
    return new Response(JSON.stringify(payload), {
      status: attempt === 1 ? 502 : 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  const request = {
    text: '花园酒店 友谊商店 盒马鲜生 万国广场',
    origin: { lng: 113.3, lat: 23.135 },
  };
  await assert.rejects(() => API.startPlanningConversation(request));
  await API.startPlanningConversation(request);

  assert.equal(keys.length, 2);
  assert.notEqual(keys[0], keys[1]);
});
