const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '..', 'templates', 'dashboard.html'), 'utf8');

function extract(start, end) {
  const first = template.indexOf(start);
  const last = template.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `Missing chart code: ${start}`);
  return template.slice(first, last);
}

test('gradient follows visible completed line bars and is hidden in candle mode', () => {
  const calls = [];
  const context = vm.createContext({
    activeChartType: 'line',
    MYT_OFFSET_SECONDS: 0,
    renderedLineBars: Array.from({length: 10000}, (_, time) => ({time, close: time * 2})),
    chart: {timeScale: () => ({
      getVisibleRange: () => ({from: 5000, to: 5002}),
      timeToCoordinate: time => time - 5000
    })},
    lineSeries: {priceToCoordinate: price => price / 2},
  });
  vm.runInContext(extract('  function createGradientLinePrimitive()', '  function updateWalletValues('), context);
  const canvas = {
    createLinearGradient: (...args) => {
      calls.push(['gradient', ...args]);
      return {addColorStop: (...stop) => calls.push(['stop', ...stop])};
    },
    save: () => calls.push(['save']),
    restore: () => calls.push(['restore']),
    beginPath: () => calls.push(['begin']),
    moveTo: (...point) => calls.push(['move', ...point]),
    lineTo: (...point) => calls.push(['line', ...point]),
    stroke: () => calls.push(['stroke']),
  };
  const renderer = vm.runInContext('createGradientLinePrimitive()', context).paneViews()[0].renderer();
  const target = {useMediaCoordinateSpace: callback => callback({context: canvas, mediaSize: {height: 400}})};
  renderer.draw(target);
  assert.deepEqual(calls.filter(call => call[0] === 'stop'), [
    ['stop', 0, '#A855F7'], ['stop', 1, '#3B82F6']
  ]);
  assert.deepEqual(calls.filter(call => call[0] === 'move' || call[0] === 'line'), [
    ['move', -1, 4999], ['line', 0, 5000], ['line', 1, 5001], ['line', 2, 5002], ['line', 3, 5003]
  ]);
  calls.length = 0;
  context.activeChartType = 'candle';
  renderer.draw(target);
  assert.deepEqual(calls, []);
});

test('feed holds last server value on failure and replaces it on reconnect', async () => {
  const error = {hidden: true};
  let fail = true;
  const live = {t: 60, dur: 60, o: 510, h: 512, l: 510, c: 512, v: 80};
  const context = vm.createContext({
    allCandles: [{t: 0, c: 510}],
    serverLiveCandle: {t: 0, c: 509},
    candleRevision: 'same',
    candleFeedClosed: false,
    candleFeedBusy: false,
    lastClosedPoll: 0,
    candlesRevision: 0,
    AbortController, setTimeout, clearTimeout,
    console: {error() {}},
    document: {getElementById: () => error},
    window: {location: {reload: () => assert.fail('Unexpected reload')}},
    draw() {}, updateHeader() {},
    fetch: async () => {
      if (fail) throw new Error('offline');
      return {ok: true, json: async () => ({
        start: 0, revision: 'same', closed: false,
        candles: [{t: 0, c: 510}], live
      })};
    },
  });
  const source = extract('  function refreshServerCandles()', '  function buildLiveCandle()')
    .replace('{{ url_for("chart_candles") }}', '/api/chart/candles');
  vm.runInContext(source, context);
  context.refreshServerCandles();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(error.hidden, false);
  assert.equal(context.serverLiveCandle.c, 509);
  assert.equal(context.candleFeedBusy, false);
  fail = false;
  context.refreshServerCandles();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(error.hidden, true);
  assert.equal(context.serverLiveCandle.c, 512);
  assert.equal(context.serverLiveCandle.t, 60);
});
