// Verifies the JSON contract: a spec written by hand must validate, revive into
// real classes, and render without touching the renderer internals.
import { chromium } from 'playwright';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';

const ROOT = path.resolve('.');
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' };
const server = http.createServer((req, res) => {
  const rel = decodeURIComponent((req.url || '/').split('?')[0]);
  const file = path.join(ROOT, rel === '/' ? 'index.html' : rel);
  fs.readFile(file, (err, data) => {
    if (err) { res.writeHead(404); res.end('not found'); return; }
    res.writeHead(200, { 'content-type': TYPES[path.extname(file)] || 'application/octet-stream' });
    res.end(data);
  });
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const { port } = server.address();

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 854, height: 480 }, deviceScaleFactor: 1 });
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push(e.message));
await page.goto(`http://127.0.0.1:${port}/index.html`, { waitUntil: 'load' });
await page.waitForFunction('window.series && window.series.ready === true', undefined, { timeout: 30000 });

// A spec that exercises a camera, a gradient, text and seeded particles: the
// parts that broke when scenes arrived as raw JSON.
const spec = {
  title: 'spec smoke test',
  scenes: [
    {
      name: 'open',
      duration: 2,
      background: '#05070d',
      backgroundGradient: {
        type: 'linear', x0: 0, y0: 0, x1: 1, y1: 1,
        stops: [{ offset: 0, color: '#0b1a3a' }, { offset: 1, color: '#05070d' }],
      },
      transitionIn: { type: 'fade', duration: 0.5 },
      transitionOut: { type: 'fade', duration: 0.5 },
      camera: { x: 960, y: 540, zoom: 1, shake: 0 },
      elements: [
        { type: 'particles', start: 0, duration: 2, count: 40, seed: 1234, color: '#7dd3fc', opacity: 0.6 },
        { type: 'text', text: 'HELLO', start: 0.2, duration: 1.5, y: 860, size: 120, fill: '#e2f4ff', easing: 'easeOut' },
        { type: 'shape', shape: 'ring', start: 0, duration: 2, x: 400, y: 400, size: 220, stroke: '#38bdf8', lineWidth: 5 },
      ],
    },
    {
      name: 'close',
      duration: 1.5,
      background: '#101820',
      camera: {
        keyframes: [
          { time: 0, x: 1100, zoom: 1.0, easing: 'easeInOut' },
          { time: 1.5, x: 820, zoom: 1.15 },
        ],
      },
      elements: [
        { type: 'shape3d', start: 0, duration: 1.5, size: 260, spin: 120, face: '#f59e0b', side: '#7c2d12' },
      ],
    },
  ],
};

const result = await page.evaluate((s) => window.series.setScenes(s), spec);
console.log('setScenes ->', JSON.stringify(result));
assert.equal(result.scenes, 2, 'both scenes should load');
assert.equal(result.totalDuration, 3.5, 'durations should sum to 3.5s');
assert.ok(!result.issues.some((i) => i.level === 'error'), 'a valid spec must not report errors');

// The camera must be a live instance, not a plain object: this is the regression
// that made a JSON spec render nothing.
const kinds = await page.evaluate(() => {
  const applied = [];
  const original = CanvasRenderingContext2D.prototype.translate;
  CanvasRenderingContext2D.prototype.translate = function (...args) { applied.push(args); return original.apply(this, args); };
  try {
    window.series.renderAtTime(1.0);
    window.series.renderAtTime(2.5);
  } finally {
    CanvasRenderingContext2D.prototype.translate = original;
  }
  return { translateCalls: applied.length };
});
console.log('transform calls while rendering two frames:', kinds.translateCalls);
assert.ok(kinds.translateCalls > 4, 'camera must contribute transform calls');

// The title must actually put bright pixels on the surface.
const lit = await page.evaluate(() => {
  window.series.renderAtTime(1.0);
  const canvas = document.getElementById('canvas');
  const ctx = canvas.getContext('2d');
  const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
  let bright = 0;
  for (let i = 0; i < data.length; i += 4) {
    if (data[i] > 180 && data[i + 1] > 180 && data[i + 2] > 180) bright += 1;
  }
  return bright;
});
console.log('near-white pixels on frame at t=1.0:', lit);
assert.ok(lit > 50, 'the title should be rendered as near-white pixels');

assert.deepEqual(pageErrors, [], 'no page errors while rendering the spec');

await browser.close();
server.close();
console.log('\nspec smoke test passed');
