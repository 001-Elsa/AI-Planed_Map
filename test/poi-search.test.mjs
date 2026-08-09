import assert from 'node:assert/strict';
import test from 'node:test';

globalThis.window = globalThis;
globalThis.location = { href: 'http://127.0.0.1:3000/' };
globalThis.document = {
  createElement: () => ({}),
  head: { appendChild: () => {} },
};

const moduleUrl = new URL('../public/js/services/amap.js', import.meta.url);

async function loadProxyModule(tag) {
  const amap = await import(moduleUrl.href + '?' + tag);
  amap.loadAMap('test-js-key', '', true, () => {}, () => {});
  return amap;
}

function poi(id, name, location) {
  return { id, name, location, address: name + '地址', distance: '100' };
}

test('代理模式批量读取两页周边 POI 并去重', async () => {
  const amap = await loadProxyModule('batch');
  const urls = [];
  globalThis.fetch = async (url) => {
    urls.push(String(url));
    const page = new URL(String(url), globalThis.location.href).searchParams.get('page');
    const pois = page === '1'
      ? [poi('a', '地点A', '116.1,39.1')]
      : [poi('a', '地点A', '116.1,39.1'), poi('b', '地点B', '116.2,39.2')];
    return new Response(JSON.stringify({ status: '1', pois }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  const rows = await amap.searchNearbyPlaces({
    keyword: '美食', type: '050000', center: { lng: 116.3, lat: 39.9 }, pages: 2,
  });

  assert.equal(urls.length, 2);
  assert.deepEqual(rows.map((item) => item.id), ['a', 'b']);
  assert.deepEqual(rows[0].location, { lng: 116.1, lat: 39.1 });
  assert.ok(urls.every((url) => url.includes('platform=JS') && url.includes('types=050000')));
});

test('细分类没有结果时自动退回关键词搜索', async () => {
  const amap = await loadProxyModule('fallback');
  const requestedTypes = [];
  globalThis.fetch = async (url) => {
    const type = new URL(String(url), globalThis.location.href).searchParams.get('types');
    requestedTypes.push(type);
    const pois = type ? [] : [poi('fallback', '关键词结果', '116.4,39.9')];
    return new Response(JSON.stringify({ status: '1', pois }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  const rows = await amap.searchNearbyPlaces({
    keyword: '卫生间', type: '200300', center: [116.3, 39.9], pages: 1,
  });

  assert.deepEqual(requestedTypes, ['200300', '']);
  assert.equal(rows[0].name, '关键词结果');
});

test('代理模式返回真实路线、公交方案和天气结构', async () => {
  const amap = await loadProxyModule('routes');
  globalThis.fetch = async (url) => {
    const pathname = new URL(String(url), globalThis.location.href).pathname;
    let payload;
    if (pathname.endsWith('/v3/direction/walking')) {
      payload = {
        status: '1',
        route: { paths: [{ distance: '1200', duration: '900', steps: [
          { instruction: '向东步行', polyline: '116.1,39.1;116.2,39.2' },
        ] }] },
      };
    } else if (pathname.endsWith('/v4/direction/bicycling')) {
      payload = {
        errcode: 0,
        data: { paths: [{ distance: '1800', duration: '420', steps: [
          { instruction: '沿道路骑行', polyline: '116.1,39.1;116.3,39.3' },
        ] }] },
      };
    } else if (pathname.endsWith('/v3/direction/transit/integrated')) {
      payload = {
        status: '1', route: { transits: [{ distance: '3200', duration: '1200', cost: '4', segments: [{
          walking: { steps: [{ instruction: '步行到车站', polyline: '116.1,39.1;116.11,39.11' }] },
          bus: { buslines: [{ name: '地铁1号线', type: '地铁', polyline: '116.11,39.11;116.3,39.3' }] },
        }] }] },
      };
    } else if (pathname.endsWith('/v3/weather/weatherInfo')) {
      payload = { status: '1', lives: [{
        city: '北京市', weather: '晴', temperature: '26', winddirection: '南', windpower: '2', humidity: '40',
      }] };
    } else {
      throw new Error('unexpected URL ' + pathname);
    }
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  const from = { lng: 116.1, lat: 39.1 };
  const to = { lng: 116.3, lat: 39.3 };
  const walking = await amap.routeLeg(from, to, 'walk');
  const riding = await amap.routeLeg(from, to, 'ride');
  const transitLeg = await amap.routeLeg(from, to, 'transit', '北京市');
  const transit = await amap.searchTransitPlans(from, to, '北京市');
  const weather = await amap.getLiveWeather('北京市');

  assert.equal(walking.ok, true);
  assert.equal(walking.distance, 1200);
  assert.equal(walking.path.length, 2);
  assert.equal(riding.ok, true);
  assert.equal(riding.time, 420);
  assert.equal(transitLeg.ok, true);
  assert.equal(transitLeg.distance, 3200);
  assert.equal(transitLeg.time, 1200);
  assert.equal(transitLeg.path.length, 4);
  assert.equal(transit[0].segments[1].transit_mode, 'SUBWAY');
  assert.equal(transit[0].cost, 4);
  assert.equal(weather.weather, '晴');
  assert.equal(weather.windDirection, '南');
});
