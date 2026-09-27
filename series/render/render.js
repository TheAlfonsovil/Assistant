import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import { createReadStream, existsSync, mkdirSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, extname, join, normalize, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DEFAULT_CONFIG, resolveConfig, totalFrames } from '../src/config.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '..');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.svg': 'image/svg+xml',
  '.woff2': 'font/woff2',
};

/**
 * Serve the app over http.
 *
 * ES modules are blocked on file:// by CORS, so the renderer needs a real
 * origin even though everything is local.
 */
function serve() {
  const server = createServer((request, response) => {
    const url = new URL(request.url, 'http://localhost');
    const relative = decodeURIComponent(url.pathname === '/' ? '/index.html' : url.pathname);
    const target = normalize(join(ROOT, relative));
    if (!target.startsWith(ROOT) || !existsSync(target) || statSync(target).isDirectory()) {
      response.writeHead(404).end('not found');
      return;
    }
    response.writeHead(200, { 'Content-Type': MIME[extname(target)] || 'application/octet-stream' });
    createReadStream(target).pipe(response);
  });
  return new Promise((resolveServer) => {
    server.listen(0, '127.0.0.1', () => resolveServer({ server, port: server.address().port }));
  });
}

/**
 * Offline render + encode.
 *
 * Steps: launch Chromium, open the page, loop over every frame setting an exact
 * time, capture the canvas only (never a full-page screenshot), then mux with
 * ffmpeg.
 */

/** Locate ffmpeg: PATH, then a local ./bin folder. */
function resolveFfmpeg() {
  if (process.env.FFMPEG_PATH && existsSync(process.env.FFMPEG_PATH)) {
    return process.env.FFMPEG_PATH;
  }
  const local = join(ROOT, 'bin', 'ffmpeg.exe');
  if (existsSync(local)) return local;
  return 'ffmpeg';
}

function log(message) {
  process.stdout.write(`[series] ${message}\n`);
}

function run(command, args) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, { stdio: ['ignore', 'pipe', 'pipe'] });
    let stderr = '';
    child.stderr.on('data', (chunk) => {
      stderr += chunk.toString();
    });
    child.on('error', (error) =>
      reject(
        error.code === 'ENOENT'
          ? new Error(`${command} not found. Install FFmpeg or set FFMPEG_PATH.`)
          : error
      )
    );
    child.on('close', (code) =>
      code === 0 ? resolvePromise() : reject(new Error(`${command} failed (${code}): ${stderr.slice(-800)}`))
    );
  });
}

/** Capture every frame as PNG. */
async function renderFrames(config, outDir, port) {
  mkdirSync(outDir, { recursive: true });
  const browser = await chromium.launch({ args: ['--force-color-profile=srgb', '--disable-lcd-text'] });
  try {
    const page = await browser.newPage({
      viewport: { width: config.width, height: config.height },
      deviceScaleFactor: 1,
    });
    page.on('pageerror', (error) => log(`page error: ${error.message}`));
    page.on('console', (message) => {
      if (message.type() === 'error') log(`console: ${message.text()}`);
    });

    await page.goto(`http://127.0.0.1:${port}/index.html`, { waitUntil: 'load' });
    try {
      await page.waitForFunction(() => window.series && window.series.ready, undefined, { timeout: 30000 });
    } catch {
      const pageError = await page.evaluate(() => window.seriesError || null);
      throw new Error(`page did not initialise${pageError ? `: ${pageError}` : ''}`);
    }
    await page.evaluate((overrides) => window.series.setConfig(overrides), {
      width: config.width,
      height: config.height,
      fps: config.fps,
    });

    const frames = totalFrames(config);
    log(`rendering ${frames} frames at ${config.width}x${config.height}@${config.fps}`);

    for (let frame = 0; frame < frames; frame += 1) {
      // Deterministic: the exact time is set, then one frame is painted.
      await page.evaluate((time) => window.series.renderAtTime(time), frame / config.fps);
      // Read the canvas backing store directly. An element screenshot would
      // return the CSS-scaled size, which is neither the requested resolution
      // nor guaranteed to have even dimensions for yuv420p.
      const dataUrl = await page.evaluate(() => document.getElementById('canvas').toDataURL('image/png'));
      const file = join(outDir, `frame_${String(frame).padStart(5, '0')}.png`);
      writeFileSync(file, Buffer.from(dataUrl.split(',')[1], 'base64'));
      if (frame % 24 === 0 || frame === frames - 1) {
        log(`frame ${frame + 1}/${frames}`);
      }
    }
    return frames;
  } finally {
    await browser.close();
  }
}

/** Mux the captured frames into the final file. */
async function encode(config, framesDir, outFile) {
  const ffmpeg = resolveFfmpeg();
  const input = join(framesDir, 'frame_%05d.png');
  const common = ['-y', '-framerate', String(config.fps), '-i', input];

  if (config.format === 'gif') {
    await run(ffmpeg, [
      ...common,
      '-vf', `fps=${config.fps},scale=${config.width}:${config.height}:flags=lanczos,palettegen=stats_mode=diff`,
      join(framesDir, 'palette.png'),
    ]);
    await run(ffmpeg, [
      '-y', '-framerate', String(config.fps), '-i', input,
      '-i', join(framesDir, 'palette.png'),
      '-lavfi', 'paletteuse=dither=bayer:bayer_scale=3',
      outFile,
    ]);
    return;
  }

  if (config.format === 'webm') {
    await run(ffmpeg, [
      ...common,
      '-c:v', 'libvpx-vp9', '-pix_fmt', 'yuv420p', '-b:v', '0', '-crf', String(config.quality),
      outFile,
    ]);
    return;
  }

  await run(ffmpeg, [
    ...common,
    '-vf', `scale=${config.width}:${config.height}:flags=lanczos`,
    '-c:v', 'libx264', '-preset', 'medium', '-crf', String(config.quality),
    '-pix_fmt', 'yuv420p', '-movflags', '+faststart', outFile,
  ]);
}

export async function exportVideo(overrides = {}) {
  const config = resolveConfig({ ...DEFAULT_CONFIG, ...overrides });
  const framesDir = join(ROOT, 'output', 'frames');
  const outFile = join(ROOT, 'output', `video.${config.format === 'gif' ? 'gif' : config.format}`);

  if (existsSync(framesDir)) rmSync(framesDir, { recursive: true, force: true });
  mkdirSync(framesDir, { recursive: true });

  const started = Date.now();
  const { server, port } = await serve();
  try {
    await renderFrames(config, framesDir, port);
  } finally {
    await new Promise((done) => server.close(done));
  }
  const count = readdirSync(framesDir).filter((f) => f.endsWith('.png')).length;
  log(`captured ${count} frames, encoding ${config.format}…`);
  await encode(config, framesDir, outFile);
  log(`done in ${((Date.now() - started) / 1000).toFixed(1)}s -> ${outFile}`);
  return { file: outFile, frames: count, config };
}

const invoked = process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url));
if (invoked) {
  exportVideo().catch((error) => {
    process.stderr.write(`[series] ${error.message}\n`);
    process.exit(1);
  });
}
