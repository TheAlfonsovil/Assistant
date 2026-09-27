/**
 * Deterministic math helpers.
 *
 * Every function here is pure: the same inputs always produce the same output,
 * so a given (seed, time) pair always renders the same frame.
 */

export const clamp = (value, min = 0, max = 1) =>
  value < min ? min : value > max ? max : value;

export const lerp = (a, b, t) => a + (b - a) * t;

export const inverseLerp = (a, b, value) => (b === a ? 0 : (value - a) / (b - a));

export const mapRange = (value, inMin, inMax, outMin, outMax) =>
  lerp(outMin, outMax, clamp(inverseLerp(inMin, inMax, value)));

export const smoothstep = (t) => {
  const x = clamp(t);
  return x * x * (3 - 2 * x);
};

export const easeIn = (t) => clamp(t) ** 2;
export const easeOut = (t) => 1 - (1 - clamp(t)) ** 2;
export const easeInOut = (t) => {
  const x = clamp(t);
  return x < 0.5 ? 2 * x * x : 1 - (-2 * x + 2) ** 2 / 2;
};
export const easeOutBack = (t) => {
  const c1 = 1.70158;
  const c3 = c1 + 1;
  const x = clamp(t);
  return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2;
};

export const EASINGS = {
  linear: (t) => clamp(t),
  easeIn,
  easeOut,
  easeInOut,
  smoothstep,
  easeOutBack,
};

/** Easing by name, with a linear fallback for unknown names. */
export const easing = (name) => EASINGS[name] || EASINGS.linear;

/**
 * mulberry32: small, fast, deterministic PRNG.
 * Used instead of Math.random so particle systems reproduce exactly.
 */
export function random(seed) {
  let state = (seed >>> 0) || 1;
  return function next() {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Deterministic helper built on top of a seeded generator. */
export function rng(seed) {
  const next = random(seed);
  return {
    next,
    range: (min, max) => lerp(min, max, next()),
    int: (min, max) => Math.floor(lerp(min, max + 1, next())),
    pick: (items) => items[Math.floor(next() * items.length) % items.length],
    sign: () => (next() < 0.5 ? -1 : 1),
  };
}

/** Hex colour helper: "#rrggbb" plus alpha -> "rgba(r, g, b, a)". */
export function rgba(hex, alpha = 1) {
  const value = hex.replace('#', '');
  const full =
    value.length === 3
      ? value.split('').map((c) => c + c).join('')
      : value;
  const int = parseInt(full, 16);
  const r = (int >> 16) & 255;
  const g = (int >> 8) & 255;
  const b = int & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/** Blend two hex colours; t=0 returns a, t=1 returns b. */
export function mixColor(a, b, t) {
  const parse = (hex) => {
    const v = hex.replace('#', '');
    const int = parseInt(v.length === 3 ? v.split('').map((c) => c + c).join('') : v, 16);
    return [(int >> 16) & 255, (int >> 8) & 255, int & 255];
  };
  const [r1, g1, b1] = parse(a);
  const [r2, g2, b2] = parse(b);
  const k = clamp(t);
  return `rgb(${Math.round(lerp(r1, r2, k))}, ${Math.round(lerp(g1, g2, k))}, ${Math.round(
    lerp(b1, b2, k)
  )})`;
}
