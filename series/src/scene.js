import { Camera, CAMERA_PRESETS } from './camera.js';
import { Scene, layoutSequential } from './timeline.js';

/**
 * Demo film: four scenes with a deliberate visual arc.
 *
 * It exists to prove the system, so it exercises the full feature set:
 * gradients, particles, glow, camera moves, easing, transitions and a 3D
 * placeholder. Scenes are plain data, so the LLM can emit the same shape.
 */

const nightGradient = {
  type: 'linear',
  x0: 0,
  y0: 0,
  x1: 0.4,
  y1: 1,
  stops: [
    { offset: 0, color: '#0b1a3a' },
    { offset: 0.55, color: '#0a1226' },
    { offset: 1, color: '#04060e' },
  ],
};

const emberGradient = {
  type: 'radial',
  x: 0.5,
  y: 0.62,
  radius: 0.8,
  stops: [
    { offset: 0, color: '#f97316' },
    { offset: 0.35, color: '#7c2d12' },
    { offset: 1, color: '#05070d' },
  ],
};

const scenes = [
  new Scene({
    name: 'intro',
    duration: 3,
    description: 'Title reveal over a slow drift through drifting particles.',
    backgroundGradient: nightGradient,
    camera: CAMERA_PRESETS.slowPushIn(),
    transitionIn: { type: 'fade', duration: 1 },
    elements: [
      {
        type: 'particles',
        start: 0,
        duration: 3,
        count: 130,
        seed: 9182,
        color: '#7dd3fc',
        minSize: 1,
        maxSize: 3.4,
        drift: 70,
        rise: 40,
        opacity: 0.55,
        easing: 'linear',
      },
      {
        type: 'text',
        text: 'SERIES',
        start: 0.4,
        duration: 2.2,
        y: 860,
        sizeFrom: 150,
        size: 128,
        letterSpacing: 26,
        fill: '#e2f4ff',
        glow: '#38bdf8',
        glowBlur: 70,
        easing: 'easeOut',
      },
      {
        type: 'text',
        text: 'render determinista · canvas 2d',
        start: 1.1,
        duration: 1.6,
        y: 990,
        size: 34,
        weight: 400,
        letterSpacing: 6,
        fill: '#7dd3fc',
        easing: 'easeOut',
      },
    ],
  }),

  new Scene({
    name: 'constellation',
    duration: 3.5,
    description: 'Shapes converge while the camera pans left.',
    background: '#060a16',
    backgroundGradient: {
      type: 'linear',
      x0: 0,
      y0: 0,
      x1: 1,
      y1: 1,
      stops: [
        { offset: 0, color: '#0a1836' },
        { offset: 1, color: '#04060e' },
      ],
    },
    camera: CAMERA_PRESETS.driftLeft(),
    elements: [
      { type: 'particles', start: 0, duration: 3.5, count: 60, seed: 4471, color: '#a78bfa', rise: 25, opacity: 0.4 },
      {
        type: 'shape',
        shape: 'ring',
        start: 0,
        duration: 3.2,
        from: { x: 520, y: 540 },
        to: { x: 900, y: 540 },
        size: 420,
        lineWidth: 5,
        stroke: '#38bdf8',
        gradient: { glow: '#0ea5e9', glowBlur: 60 },
        easing: 'easeInOut',
      },
      {
        type: 'shape',
        shape: 'circle',
        start: 0.3,
        duration: 2.9,
        from: { x: 1400, y: 500 },
        to: { x: 1060, y: 560 },
        sizeFrom: 40,
        size: 150,
        fill: '#f0abfc',
        gradient: { glow: '#c026d3', glowBlur: 80 },
        easing: 'easeOut',
      },
      {
        type: 'shape',
        shape: 'triangle',
        start: 0.6,
        duration: 2.7,
        from: { x: 900, y: 1180 },
        to: { x: 960, y: 800 },
        size: 90,
        fill: '#34d399',
        spin: 90,
        gradient: { glow: '#10b981', glowBlur: 50 },
        easing: 'easeOutBack',
      },
    ],
  }),

  new Scene({
    name: 'core',
    duration: 3,
    description: 'Lit cube turns under a handheld camera; copy lands in sequence.',
    backgroundGradient: emberGradient,
    camera: CAMERA_PRESETS.handheld(),
    elements: [
      { type: 'particles', start: 0, duration: 3, count: 80, seed: 7777, color: '#fed7aa', rise: 120, minSize: 1, maxSize: 3, opacity: 0.5 },
      {
        type: 'shape3d',
        start: 0,
        duration: 3,
        size: 300,
        spin: 150,
        face: '#f59e0b',
        side: '#7c2d12',
        easing: 'easeInOut',
      },
      {
        type: 'text',
        text: 'ACTO II',
        start: 0.4,
        duration: 1.8,
        y: 1180,
        size: 58,
        letterSpacing: 18,
        fill: '#ffedd5',
        glow: '#f97316',
        glowBlur: 50,
        easing: 'easeOut',
      },
      {
        type: 'shape',
        shape: 'rect',
        start: 0.9,
        duration: 1.4,
        x: 960,
        y: 1180,
        width: 420,
        height: 2,
        fill: '#fdba74',
        easing: 'easeOut',
      },
    ],
  }),

  new Scene({
    name: 'outro',
    duration: 2.5,
    description: 'Camera pulls back to reveal the final card.',
    background: '#04060e',
    backgroundGradient: nightGradient,
    camera: CAMERA_PRESETS.pullBack(),
    elements: [
      { type: 'particles', start: 0, duration: 2.5, count: 150, seed: 2024, color: '#38bdf8', rise: 150, opacity: 0.45 },
      {
        type: 'shape',
        shape: 'rect',
        start: 0,
        duration: 2.4,
        x: 960,
        y: 540,
        widthFrom: 0,
        width: 760,
        height: 200,
        radius: 24,
        fill: 'rgba(15,23,42,0.72)',
        stroke: '#1e40af',
        lineWidth: 3,
        easing: 'easeOut',
      },
      {
        type: 'text',
        text: 'FIN',
        start: 0.5,
        duration: 1.8,
        y: 540,
        size: 96,
        letterSpacing: 14,
        fill: '#e0f2fe',
        glow: '#0ea5e9',
        glowBlur: 60,
        easing: 'easeOutBack',
      },
    ],
  }),
];

layoutSequential(scenes);

export const DEMO_SCENES = scenes;
export const DEMO_DURATION = scenes.reduce((sum, scene) => sum + scene.duration, 0);
export { Camera };
