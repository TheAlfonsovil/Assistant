import { chromium } from 'playwright';
import { createReadStream, existsSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { createHash } from 'node:crypto';
import { dirname, extname, join, normalize, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = dirname(fileURLToPath(import.meta.url)); // this file lives at series/
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png' };

function serve() {
  const server = createServer((request, response) => {
    const url = new URL(request.url, 'http://localhost');
    const rel = decodeURIComponent(url.pathname === '/' ? '/index.html' : url.pathname);
    const target = normalize(join(ROOT, rel));
    if (!target.startsWith(ROOT) || !existsSync(target) || statSync(target).isDirectory()) {
      response.writeHead(404).end('no');
      return;
    }
    response.writeHead(200, { 'Content-Type': MIME[extname(target)] || 'application/octet-stream' });
    createReadStream(target).pipe(response);
  });
  return new Promise((r) => server.listen(0, '127.0.0.1', () => r({ server, port: server.address().port })));
}

const TIMES = [0, 60 / 24, 287 / 24];

const browser = await chromium.launch();
const { server, port } = await serve();
try {
  // Two independent browser sessions: same seed and time must hash equally.
  for (const pass of [1, 2]) {
    const page = await browser.newPage({ viewport: { width: 854, height: 480 } });
    page.on('pageerror', (e) => console.log('PAGE ERROR:', e.message));
    page.on('console', (m) => { if (m.type() === 'error') console.log('CONSOLE:', m.text()); });
    await page.goto(`http://127.0.0.1:${port}/index.html`, { waitUntil: 'load' });
    try {
      await page.waitForFunction(() => window.series && window.series.ready, undefined, { timeout: 20000 });
    } catch {
      console.log('seriesError =', await page.evaluate(() => window.seriesError));
      console.log('has canvas =', await page.evaluate(() => !!document.getElementById('canvas')));
      throw new Error('not ready');
    }
    const hashes = [];
    for (const time of TIMES) {
      const url = await page.evaluate((t) => {
        window.series.renderAtTime(t);
        return document.getElementById('canvas').toDataURL('image/png');
      }, time);
      hashes.push(createHash('sha256').update(Buffer.from(url.split(',')[1], 'base64')).digest('hex').slice(0, 16));
    }
    console.log(`pass ${pass}:`, hashes.join(' '));
    await page.close();
  }
} finally {
  await browser.close();
  await new Promise((done) => server.close(done));
}
