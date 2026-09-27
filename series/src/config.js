/**
 * Central configuration.
 *
 * Resolution is deliberately modest by default: the renderer is CPU-bound
 * (Canvas 2D in headless Chromium), so a bigger canvas costs linearly.
 */

export const DEFAULT_CONFIG = {
  width: 854,
  height: 480,
  fps: 24,
  duration: 12,
  format: 'mp4',
  seed: 20240501,
  quality: 23, // CRF: lower is better quality and a bigger file.
  background: '#05070d',
};

/** Merge a partial config over the defaults, keeping unknown keys intact. */
export function resolveConfig(overrides = {}) {
  const config = { ...DEFAULT_CONFIG, ...overrides };
  config.width = Math.max(2, Math.round(config.width / 2) * 2); // H.264 needs even dims.
  config.height = Math.max(2, Math.round(config.height / 2) * 2);
  config.fps = Math.max(1, Math.round(config.fps));
  config.duration = Math.max(0.1, config.duration);
  return config;
}

export const totalFrames = (config) => Math.max(1, Math.round(config.fps * config.duration));

export const frameToTime = (config, frame) => frame / config.fps;

export const timeToFrame = (config, time) => Math.round(time * config.fps);

/**
 * Resolution-independent coordinate system.
 *
 * Scenes are authored against a 1920x1080 design space; the scale maps that
 * onto the real output, so the same scene renders at 854x480 or 1920x1080
 * without touching the animation code.
 */
export function createViewport(config) {
  const DESIGN_WIDTH = 1920;
  const DESIGN_HEIGHT = 1080;
  const scaleX = config.width / DESIGN_WIDTH;
  const scaleY = config.height / DESIGN_HEIGHT;
  return {
    width: config.width,
    height: config.height,
    designWidth: DESIGN_WIDTH,
    designHeight: DESIGN_HEIGHT,
    scaleX,
    scaleY,
    /** Scale a design-space length. Uses the mean axis to avoid distortion. */
    unit: (value) => value * Math.min(scaleX, scaleY),
    /** Map a design-space x coordinate to pixels. */
    x: (value) => value * scaleX,
    /** Map a design-space y coordinate to pixels. */
    y: (value) => value * scaleY,
    /** Centre point in pixels. */
    cx: config.width / 2,
    cy: config.height / 2,
    /**
     * Centre point in DESIGN space. Element renderers draw in design units, so
     * they must anchor here and not to the pixel centre: using cx/cy would put
     * a 1920x1080 composition in the top-left quadrant of the frame.
     */
    designCx: DESIGN_WIDTH / 2,
    designCy: DESIGN_HEIGHT / 2,
  };
}
