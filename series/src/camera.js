import { clamp, easing, lerp, rng } from './math.js';

/**
 * Virtual camera.
 *
 * A camera is a pure function of time: it owns pan, zoom, rotation, dolly and
 * shake, and exposes them as a canvas transform. Nothing here reads the wall
 * clock, so export and preview see identical frames.
 */
export class Camera {
  constructor({
    x = 960,
    y = 540,
    zoom = 1,
    rotation = 0,
    dolly = 0,
    shake = 0,
    shakeSpeed = 18,
    seed = 7,
    easing: easeName = 'easeInOut',
    keyframes = null,
  } = {}) {
    this.x = x;
    this.y = y;
    this.zoom = zoom;
    this.rotation = rotation;
    this.dolly = dolly;
    this.shake = shake;
    this.shakeSpeed = shakeSpeed;
    this.seed = seed;
    this.easing = easeName;
    this.keyframes = keyframes;
  }

  /** Interpolated state at an absolute time. */
  stateAt(time) {
    if (this.keyframes && this.keyframes.length) {
      return this.fromKeyframes(time);
    }
    const ease = easing(this.easing);
    // A slow sine gives a continuous, loop-free drift without randomness.
    const drift = Math.sin(time * 0.35) * 0.5 + Math.sin(time * 0.17) * 0.5;
    return {
      x: this.x + drift * 26,
      y: this.y + Math.cos(time * 0.23) * 14,
      zoom: this.zoom * (1 + ease((time % 8) / 8) * 0.04),
      rotation: this.rotation + drift * 0.35,
      dolly: this.dolly + drift * 40,
      shake: this.shake,
    };
  }

  fromKeyframes(time) {
    const frames = this.keyframes;
    if (time <= frames[0].time) return { ...frames[0] };
    const last = frames[frames.length - 1];
    if (time >= last.time) return { ...last };
    for (let i = 0; i < frames.length - 1; i += 1) {
      const a = frames[i];
      const b = frames[i + 1];
      if (time >= a.time && time <= b.time) {
        const raw = (time - a.time) / (b.time - a.time);
        const t = easing(a.easing || 'easeInOut')(raw);
        return {
          x: lerp(a.x ?? this.x, b.x ?? this.x, t),
          y: lerp(a.y ?? this.y, b.y ?? this.y, t),
          zoom: lerp(a.zoom ?? this.zoom, b.zoom ?? this.zoom, t),
          rotation: lerp(a.rotation ?? this.rotation, b.rotation ?? this.rotation, t),
          dolly: lerp(a.dolly ?? this.dolly, b.dolly ?? this.dolly, t),
          shake: lerp(a.shake ?? this.shake, b.shake ?? this.shake, t),
        };
      }
    }
    return { ...last };
  }

  /**
   * Deterministic handheld shake.
   * Each channel is a sum of sines seeded from the same generator, so the
   * motion is irregular but perfectly reproducible.
   */
  shakeOffset(time) {
    const strength = this.stateAt(time).shake || 0;
    if (strength <= 0) return { x: 0, y: 0, angle: 0 };
    const noise = rng(Math.floor(this.seed * 7919) + 13);
    const a1 = noise.range(0.6, 1.4);
    const a2 = noise.range(0.6, 1.4);
    const a3 = noise.range(0.6, 1.4);
    const s = this.shakeSpeed;
    return {
      x: (Math.sin(time * s * a1) + Math.sin(time * s * a2 * 1.7)) * 0.5 * strength,
      y: (Math.cos(time * s * a1 * 1.3) + Math.sin(time * s * a3 * 2.1)) * 0.5 * strength,
      angle: Math.sin(time * s * 0.7) * 0.25 * strength,
    };
  }

  /**
   * Layer the camera on top of the base design-space transform.
   *
   * The base transform already maps design units onto the surface linearly with
   * its origin at the top-left, so design (960, 540) lands on the frame centre.
   * Zoom therefore has to be applied *about* that point: translate to the
   * (panned) centre, scale and rotate, then translate back. Omitting the first
   * translate scales about the origin and throws the whole frame off-screen.
   */
  apply(ctx, time, viewport) {
    const state = this.stateAt(time);
    const shake = this.shakeOffset(time);
    const zoom = (state.zoom || 1) * (1 + (state.dolly || 0) / 4000);
    const designCx = viewport.designWidth / 2;
    const designCy = viewport.designHeight / 2;
    // A keyframed x/y is an absolute design position; the pan is its offset
    // from the centre of frame.
    const dx = (state.x ?? designCx) - designCx + shake.x;
    const dy = (state.y ?? designCy) - designCy + shake.y;
    ctx.translate(designCx + dx, designCy + dy);
    ctx.rotate(((state.rotation || 0) + shake.angle) * (Math.PI / 180));
    ctx.scale(zoom, zoom);
    ctx.translate(-designCx, -designCy);
  }
}

/** Camera presets used by the demo scenes. */
export const CAMERA_PRESETS = {
  static: () => new Camera({ x: 960, y: 540, zoom: 1 }),
  slowPushIn: () =>
    new Camera({ x: 960, y: 540, zoom: 1, keyframes: [
      { time: 0, zoom: 1, x: 960, y: 540 },
      { time: 6, zoom: 1.18, x: 960, y: 520, easing: 'easeInOut' },
    ] }),
  driftLeft: () =>
    new Camera({ x: 960, y: 540, zoom: 1.1, keyframes: [
      { time: 0, x: 1180, y: 560, zoom: 1.14 },
      { time: 6, x: 760, y: 520, zoom: 1.06, easing: 'easeInOut' },
    ] }),
  handheld: () => new Camera({ x: 960, y: 540, zoom: 1.04, shake: 6, shakeSpeed: 15 }),
  pullBack: () =>
    new Camera({ x: 960, y: 540, zoom: 1, keyframes: [
      { time: 0, zoom: 1.3, x: 960, y: 540 },
      { time: 5, zoom: 0.94, x: 960, y: 560, easing: 'easeOut' },
    ] }),
};
