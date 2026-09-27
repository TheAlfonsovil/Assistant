import { resolveConfig, createViewport, totalFrames } from './config.js';
import { DEMO_SCENES, DEMO_DURATION } from './scene.js';
import { Renderer } from './renderer.js';
import { specToScenes, validateSpec, summarizeIssues } from './spec.js';

/**
 * Application entry point.
 *
 * The same module powers the browser preview and the headless export: both call
 * renderFrame(time), and neither path uses a wall clock to decide the state.
 */

const canvas = document.getElementById('canvas');
const ctx = canvas.getContext('2d', { alpha: false });

let config = resolveConfig();
let scenes = DEMO_SCENES;
let renderer = new Renderer(createViewport(config));
let currentTime = 0;
let rafId = null;
let playingSince = null;
let playingFrom = 0;

/** Apply the config to the canvas and rebuild the renderer. */
function applyConfig() {
  canvas.width = config.width;
  canvas.height = config.height;
  canvas.style.width = `${Math.min(config.width, 1000)}px`;
  renderer = new Renderer(createViewport(config));
}

/** Paint one frame. Pure with respect to time. */
export function renderFrame(time) {
  renderer.render(ctx, time, { scenes, config });
  return { time, frame: Math.round(time * config.fps) };
}

/* ---------------------------------------------------------------- preview */

function totalDuration() {
  return scenes.reduce((sum, scene) => sum + scene.duration, 0);
}

function drawHud() {
  const frame = Math.round(currentTime * config.fps);
  const total = Math.round(totalDuration() * config.fps);
  document.getElementById('hud').textContent =
    `t=${currentTime.toFixed(2)}s · frame ${frame}/${total} · ${config.width}x${config.height}@${config.fps}`;
  document.getElementById('seek').value = String(
    Math.round((currentTime / totalDuration()) * 1000)
  );
}

function tick(now) {
  if (playingSince === null) return;
  currentTime = playingFrom + (now - playingSince) / 1000;
  if (currentTime >= totalDuration()) {
    currentTime = totalDuration() - 0.001;
    stop();
  }
  renderFrame(currentTime);
  drawHud();
  rafId = requestAnimationFrame(tick);
}

function play() {
  if (rafId) return;
  playingFrom = currentTime;
  playingSince = performance.now();
  rafId = requestAnimationFrame(tick);
}

function stop() {
  if (rafId) cancelAnimationFrame(rafId);
  rafId = null;
  playingSince = null;
}

function renderSceneList() {
  const host = document.getElementById('scenes');
  host.innerHTML = '';
  let cursor = 0;
  scenes.forEach((scene) => {
    const item = document.createElement('div');
    item.className = 'scene-item';
    const active = currentTime >= cursor && currentTime < cursor + scene.duration;
    if (active) item.classList.add('active');
    item.innerHTML = `<span>${scene.name}</span><span class="hud">${scene.duration.toFixed(1)}s</span>`;
    item.onclick = () => {
      currentTime = cursor + 0.01;
      renderFrame(currentTime);
      drawHud();
      renderSceneList();
    };
    host.appendChild(item);
    cursor += scene.duration;
  });
}

function bindControls() {
  document.getElementById('play').onclick = play;
  document.getElementById('pause').onclick = stop;
  document.getElementById('restart').onclick = () => {
    stop();
    currentTime = 0;
    renderFrame(0);
    drawHud();
    renderSceneList();
  };
  document.getElementById('seek').oninput = (event) => {
    stop();
    currentTime = (Number(event.target.value) / 1000) * totalDuration();
    renderFrame(currentTime);
    drawHud();
  };
  const rebind = (id, key) => {
    document.getElementById(id).onchange = (event) => {
      config = resolveConfig({ ...config, [key]: Number(event.target.value) });
      applyConfig();
      renderFrame(currentTime);
      drawHud();
    };
  };
  rebind('fps', 'fps');
  rebind('width', 'width');
  rebind('height', 'height');
  document.getElementById('format').onchange = (event) => {
    config = resolveConfig({ ...config, format: event.target.value });
  };
  document.getElementById('export').onclick = () => {
    document.getElementById('status').textContent =
      'Ejecuta "npm run export" para generar el MP4.';
  };
}

/* ------------------------------------------------- headless export bridge */

// The Playwright renderer calls this; it must not depend on any UI state.
window.series = {
  getConfig: () => ({ ...config }),
  totalFrames: () => totalFrames(config),
  setConfig(overrides) {
    config = resolveConfig({ ...config, ...overrides });
    applyConfig();
    return { ...config };
  },
  setScenes(next) {
    // Scenes arrive as plain JSON. They have to be revived: the renderer calls
    // camera.apply() and scene.localTime(), which only exist on the classes.
    // Spreading the raw object was enough to load it but broke on frame one.
    const { scenes: revived, issues, totalDuration } = specToScenes(next);
    scenes = revived;
    currentTime = Math.min(currentTime, totalDuration);
    return { scenes: scenes.length, totalDuration, issues: summarizeIssues(issues) };
  },
  /** Validate without applying, so the editor can reject a bad spec early. */
  validateScenes: (next) => {
    const { ok, issues } = validateSpec(next);
    return { ok, issues: summarizeIssues(issues) };
  },
  loadDemo() {
    scenes = DEMO_SCENES;
    return { scenes: scenes.length, duration: DEMO_DURATION };
  },
  renderAtTime: (time) => renderFrame(time),
  renderAtFrame: (frame) => renderFrame(frame / config.fps),
  ready: true,
};

if (document.getElementById('canvas')) {
  try {
    config.duration = totalDuration();
    applyConfig();
    bindControls();
    renderFrame(0);
    drawHud();
    renderSceneList();
    window.series.ready = true;
  } catch (error) {
    document.getElementById('status').textContent = `Error: ${error.message}`;
    window.seriesError = error.message;
  }
}
