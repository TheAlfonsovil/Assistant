import { Scene, layoutSequential } from './timeline.js';
import { Camera } from './camera.js';
import { ELEMENT_RENDERERS } from './renderer.js';

/**
 * Series spec: the JSON contract between the LLM and the renderer.
 *
 * The worker emits plain JSON. It never emits code, so nothing it produces can
 * execute, and a spec can be validated, diffed and stored before it is ever
 * rendered. Reviving turns that JSON back into Scene/Camera instances, which
 * matters because the renderer calls methods on both.
 */

export const DESIGN_WIDTH = 1920;
export const DESIGN_HEIGHT = 1080;

/** Element types the renderer can actually draw. */
export const SUPPORTED_ELEMENT_TYPES = Object.keys(ELEMENT_RENDERERS);

/** Known easing names; an unknown one silently falls back to linear. */
const KNOWN_EASINGS = new Set([
  'linear', 'easeIn', 'easeOut', 'easeInOut', 'smoothstep', 'easeOutBack',
]);

class Issue {
  constructor(level, path, message) {
    this.level = level;
    this.path = path;
    this.message = message;
  }

  toString() {
    return `${this.level}: ${this.path}: ${this.message}`;
  }
}

const error = (path, message) => new Issue('error', path, message);
const warning = (path, message) => new Issue('warning', path, message);

const isFiniteNumber = (value) => typeof value === 'number' && Number.isFinite(value);

/** Flag a design-space coordinate that is likely to fall outside the frame. */
function checkCoordinate(issues, path, value, limit) {
  if (value === undefined || value === null) return;
  if (!isFiniteNumber(value)) {
    issues.push(error(path, `expected a number, got ${JSON.stringify(value)}`));
    return;

  }
  const bound = limit * 1.5; // overshoot is legitimate, it is an animation
  if (value < -bound || value > bound) {
    issues.push(warning(path, `${value} is far outside 0..${limit}; it may be off-frame`));
  }
}

/**
 * Accept a bare array as shorthand for `{ scenes: [...] }`.
 * The worker prompt documents the object form, but an external caller written
 * against the earlier editor API would pass an array, and failing on that would
 * look like a spec bug rather than a shape mismatch.
 */
export function normalizeSpec(input) {
  return Array.isArray(input) ? { scenes: input } : input;
}

/**
 * Validate a raw spec without rendering it.
 * Errors block rendering; warnings are reported but do not stop the pipeline.
 */
export function validateSpec(rawSpec) {
  const issues = [];
  const spec = normalizeSpec(rawSpec);
  if (!spec || typeof spec !== 'object') {
    return { ok: false, issues: [error('$', 'spec must be an object')], scenes: [], totalDuration: 0 };
  }
  if (!Array.isArray(spec.scenes) || spec.scenes.length === 0) {
    return { ok: false, issues: [error('scenes', 'at least one scene is required')], scenes: [], totalDuration: 0 };
  }

  spec.scenes.forEach((scene, index) => {
    const at = `scenes[${index}]`;
    if (!scene || typeof scene !== 'object') {
      issues.push(error(at, 'scene must be an object'));
      return;
    }
    if (!isFiniteNumber(scene.duration) || scene.duration <= 0) {
      issues.push(error(`${at}.duration`, 'duration must be a positive number'));
    }
    if (!Array.isArray(scene.elements) || scene.elements.length === 0) {
      issues.push(error(`${at}.elements`, 'a scene needs at least one element'));
      return;
    }

    scene.elements.forEach((element, ei) => {
      const ep = `${at}.elements[${ei}]`;
      if (!element || typeof element !== 'object') {
        issues.push(error(ep, 'element must be an object'));
        return;
      }
      if (!SUPPORTED_ELEMENT_TYPES.includes(element.type)) {
        issues.push(error(`${ep}.type`, `unsupported type ${JSON.stringify(element.type)}; supported: ${SUPPORTED_ELEMENT_TYPES.join(', ')}`));
        return;
      }
      if (isFiniteNumber(element.duration) && element.duration < 0) {
        issues.push(error(`${ep}.duration`, 'duration must not be negative'));
      }
      if (element.easing && !KNOWN_EASINGS.has(element.easing)) {
        issues.push(warning(`${ep}.easing`, `unknown easing "${element.easing}"; it will behave as linear`));
      }
      if (element.type === 'text' && !element.text) {
        issues.push(error(`${ep}.text`, 'text elements need a text value'));
      }
      // Particles must be seeded: an unseeded system is not reproducible.
      if (element.type === 'particles' && !isFiniteNumber(element.seed)) {
        issues.push(warning(`${ep}.seed`, 'particles without a seed are not reproducible'));
      }
      checkCoordinate(issues, `${ep}.x`, element.x, DESIGN_WIDTH);
      checkCoordinate(issues, `${ep}.y`, element.y, DESIGN_HEIGHT);
    });

    if (scene.camera) {
      const camera = scene.camera;
      if (camera.keyframes !== undefined && !Array.isArray(camera.keyframes)) {
        issues.push(error(`${at}.camera.keyframes`, 'keyframes must be an array'));
      }
      if (isFiniteNumber(camera.zoom) && camera.zoom <= 0) {
        issues.push(error(`${at}.camera.zoom`, 'zoom must be greater than zero'));
      }
    }
  });

  const totalDuration = spec.scenes.reduce((sum, s) => sum + (s.duration || 0), 0);
  if (totalDuration > 90) {
    issues.push(warning('scenes', `total duration ${totalDuration.toFixed(1)}s is long; rendering is CPU-bound`));
  }

  const ok = !issues.some((i) => i.level === 'error');
  return { ok, issues, scenes: spec.scenes, totalDuration };
}

/**
 * Turn validated JSON into live Scene/Camera objects.
 * The camera is rebuilt as a real Camera because Scene.camera.apply() is a
 * method; a plain object revived from JSON would throw on the very first frame.
 */
export function reviveSpec(rawSpec) {
  const spec = normalizeSpec(rawSpec);
  const scenes = (spec.scenes || []).map((raw) => new Scene({
    ...raw,
    start: raw.start ?? 0,
    camera: raw.camera ? new Camera({ ...raw.camera }) : null,
    elements: (raw.elements || []).map((element) => ({ ...element })),
  }));
  return layoutSequential(scenes);
}

/** Validate and revive in one step. Throws on a spec that cannot be rendered. */
export function specToScenes(spec) {
  const { ok, issues, totalDuration } = validateSpec(spec);
  if (!ok) {
    const detail = issues.filter((i) => i.level === 'error').map(String).join('\n');
    throw new Error(`invalid series spec:\n${detail}`);
  }
  return { scenes: reviveSpec(spec), issues, totalDuration };
}

/** Compact form of the issues, for API responses and logs. */
export const summarizeIssues = (issues = []) =>
  issues.map((i) => ({ level: i.level, path: i.path, message: i.message }));
