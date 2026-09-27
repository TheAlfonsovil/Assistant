import { clamp, easing, mapRange } from './math.js';

/**
 * Timeline model.
 *
 * A scene is data, not code: start/duration/easing/elements. That keeps
 * rendering a pure function of time and lets the editor expose properties
 * without re-running the LLM.
 */

export class Scene {
  constructor({
    name,
    start = 0,
    duration = 3,
    background = null,
    backgroundGradient = null,
    transitionIn = { type: 'fade', duration: 0.6 },
    transitionOut = { type: 'fade', duration: 0.6 },
    elements = [],
    camera = null,
    audio = null,
    description = '',
  } = {}) {
    this.name = name;
    this.start = start;
    this.duration = duration;
    this.background = background;
    this.backgroundGradient = backgroundGradient;
    this.transitionIn = transitionIn;
    this.transitionOut = transitionOut;
    this.elements = elements;
    this.camera = camera;
    this.audio = audio;
    this.description = description;
  }

  get end() {
    return this.start + this.duration;
  }

  /** Local time in [0, duration] when the global time is inside this scene. */
  localTime(time) {
    return time - this.start;
  }

  contains(time) {
    return time >= this.start && time < this.end;
  }
}

/** Sequential layout: scene i starts where the previous one ended. */
export function layoutSequential(scenes) {
  let cursor = 0;
  for (const scene of scenes) {
    scene.start = cursor;
    cursor += scene.duration;
  }
  return scenes;
}

/**
 * Progress of one element at a given local time, plus its eased values.
 * Returns null when the element is outside its own active window.
 */
export function elementState(element, localTime) {
  const start = element.start ?? 0;
  const duration = element.duration ?? 1;
  const delay = element.delay ?? 0;
  const window = start + delay;
  const end = window + duration;
  if (localTime < window || localTime > end) return null;

  const raw = duration <= 0 ? 1 : clamp((localTime - window) / duration);
  const ease = easing(element.easing || 'easeInOut');
  return {
    /** 0..1 through the element's whole life (no enter/exit separation). */
    t: raw,
    /** Eased value for property interpolation. */
    e: ease(raw),
    /** 0 at the very start of the window, 1 at the end. */
    progress: raw,
    inWindow: true,
  };
}

/** Opacity envelope combining fade-in and fade-out around the active window. */
export function envelope(element, localTime, state) {
  let alpha = state ? state.e : 0;
  const fadeIn = element.fadeIn ?? 0.2;
  const fadeOut = element.fadeOut ?? 0.2;
  const start = (element.start ?? 0) + (element.delay ?? 0);
  const duration = element.duration ?? 1;
  const end = start + duration;
  if (!state) return 0;
  if (fadeIn > 0) alpha *= mapRange(localTime, start, start + fadeIn, 0, 1);
  if (fadeOut > 0) alpha *= mapRange(localTime, end - fadeOut, end, 1, 0);
  return clamp(alpha);
}
