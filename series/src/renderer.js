import { clamp, lerp, rgba, rng } from './math.js';
import { elementState, envelope } from './timeline.js';

/**
 * Canvas 2D renderer.
 *
 * The only public entry point is render(ctx, time, context). It never reads a
 * clock, so render(1/fps) always produces the same image for frame 1.
 */

export class Renderer {
  constructor(viewport) {
    this.viewport = viewport;
    this._images = new Map();
  }

  clear(ctx) {
    const { width, height, background } = this.viewport;
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = background || '#05070d';
    ctx.fillRect(0, 0, width, height);
    ctx.restore();
  }

  /**
   * Paint one frame.
   * @param {CanvasRenderingContext2D} ctx
   * @param {number} time absolute seconds
   * @param {{scenes: Scene[], config: object}} context
   */
  render(ctx, time, { scenes }) {
    this.clear(ctx);
    const active = scenes.filter((scene) => scene.contains(time));
    const list = active.length ? active : scenes.slice(-1);
    list.forEach((scene, index) => {
      this.renderScene(ctx, scene, scene.localTime(time), {
        blend: index === 0 ? 1 : 0.55,
        edge: index === list.length - 1,
        first: index === 0,
      });
    });
  }

  renderScene(ctx, scene, localTime, { blend = 1, edge = true, first = false }) {
    const viewport = this.viewport;
    const alpha = this.sceneAlpha(scene, localTime, { edge, first });

    ctx.save();
    ctx.globalAlpha = clamp(alpha * blend);
    this.paintBackground(ctx, scene);

    // Always enter the 1920x1080 design space first, so element coordinates
    // are resolution independent. The camera then works in that same space.
    ctx.save();
    ctx.translate(viewport.cx, viewport.cy);
    ctx.scale(viewport.scaleX, viewport.scaleY);
    ctx.translate(-viewport.designWidth / 2, -viewport.designHeight / 2);
    if (scene.camera) {
      scene.camera.apply(ctx, scene.start + localTime, viewport);
    }
    this.motionBlur(ctx, scene, localTime);
    for (const element of scene.elements) {
      this.renderElement(ctx, element, localTime, scene);
    }
    ctx.restore();
    ctx.restore();
  }

  /** In/out transition opacity for a scene. */
  sceneAlpha(scene, localTime, { edge, first }) {
    const inDur = scene.transitionIn?.duration ?? 0;
    const outDur = edge ? scene.transitionOut?.duration ?? 0 : 0;
    let alpha = 1;
    // The opening scene fades in from black on the background, not on the
    // content: a zero alpha here would make the very first frame fully black.
    if (inDur > 0 && !first) alpha *= clamp(localTime / inDur);
    if (outDur > 0) alpha *= clamp((scene.duration - localTime) / outDur);
    return clamp(alpha);
  }

  /** Subtle streaks keyed to camera dolly, to fake motion blur cheaply. */
  motionBlur(ctx, scene, localTime) {
    if (!scene.camera) return;
    const dolly = scene.camera.stateAt(scene.start + localTime).dolly || 0;
    const strength = clamp(Math.abs(dolly) / 900) * 0.16;
    if (strength < 0.01) return;
    const { designWidth: w, designHeight: h } = this.viewport;
    const gradient = ctx.createLinearGradient(0, 0, 0, h);
    gradient.addColorStop(0, `rgba(255,255,255,${strength * 0.5})`);
    gradient.addColorStop(0.5, 'rgba(255,255,255,0)');
    gradient.addColorStop(1, `rgba(0,0,0,${strength})`);
    ctx.save();
    ctx.globalCompositeOperation = 'overlay';
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, w, h);
    ctx.restore();
  }

  paintBackground(ctx, scene) {
    const { width, height } = this.viewport;
    const paint = (color) => {
      ctx.save();
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.fillStyle = color;
      ctx.fillRect(0, 0, width, height);
      ctx.restore();
    };
    if (scene.background) paint(scene.background);
    const g = scene.backgroundGradient;
    if (!g) return;
    const gradient =
      g.type === 'radial'
        ? ctx.createRadialGradient(
            (g.x ?? 0.5) * width,
            (g.y ?? 0.5) * height,
            0,
            (g.x ?? 0.5) * width,
            (g.y ?? 0.5) * height,
            (g.radius ?? 0.7) * Math.max(width, height)
          )
        : ctx.createLinearGradient(
            (g.x0 ?? 0) * width,
            (g.y0 ?? 0) * height,
            (g.x1 ?? 1) * width,
            (g.y1 ?? 1) * height
          );
    (g.stops || []).forEach((stop) => gradient.addColorStop(stop.offset, stop.color));
    paint(gradient);
  }

  renderElement(ctx, element, localTime, scene) {
    const state = elementState(element, localTime);
    if (!state) return;
    const alpha = envelope(element, localTime, state);
    if (alpha <= 0.001) return;
    const fn = ELEMENT_RENDERERS[element.type];
    if (!fn) return;
    // Element renderers are plain functions; give them viewport and image
    // access through the context instead of a long parameter list.
    ctx.__viewport = this.viewport;
    ctx.__getImage = (src) => this.getImage(src);
    ctx.save();
    fn(ctx, element, state, localTime, scene, alpha);
    ctx.restore();
  }

  /** Cache for image elements, keyed by src. */
  getImage(src) {
    if (!this._images.has(src)) {
      const img = new Image();
      img.src = src;
      this._images.set(src, img);
    }
    return this._images.get(src);
  }
}

// ELEMENT_RENDERERS is defined after the class; see below.
Renderer.ELEMENT_RENDERERS = null;

function roundRect(ctx, x, y, w, h, r) {
  const radius = Math.min(r || 0, Math.abs(w) / 2, Math.abs(h) / 2);
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.arcTo(x + w, y, x + w, y + h, radius);
  ctx.arcTo(x + w, y + h, x, y + h, radius);
  ctx.arcTo(x, y + h, x, y, radius);
  ctx.arcTo(x, y, x + w, y, radius);
  ctx.closePath();
}

/** Draw text with manual letter spacing (canvas letterSpacing is patchy). */
function drawTracked(ctx, text, x, y, spacing) {
  const chars = [...text];
  const widths = chars.map((c) => ctx.measureText(c).width);
  const total = widths.reduce((a, b) => a + b, 0) + spacing * (chars.length - 1);
  const align = ctx.textAlign;
  let cursor = align === 'center' ? x - total / 2 : x;
  ctx.textAlign = 'left';
  chars.forEach((c, i) => {
    ctx.fillText(c, cursor, y);
    cursor += widths[i] + spacing;
  });
  ctx.textAlign = align;
}

export { roundRect, drawTracked };

/**
 * Element renderers, keyed by element type.
 * Each receives (ctx, element, state, localTime, scene, alpha) and draws in the
 * 1920x1080 design space. Viewport and image access hang off ctx.__viewport and
 * ctx.__getImage so these stay plain functions.
 */
export const ELEMENT_RENDERERS = {
  shape(ctx, element, state) {
    const { designCx, designCy } = ctx.__viewport;
    const x = lerp(element.from?.x ?? element.x ?? designCx, element.to?.x ?? element.x ?? designCx, state.e);
    const y = lerp(element.from?.y ?? element.y ?? designCy, element.to?.y ?? element.y ?? designCy, state.e);
    const size = lerp(element.sizeFrom ?? element.size ?? 90, element.size ?? 90, state.e);
    const rotation = (element.rotation ?? 0) + state.e * (element.spin ?? 0);

    ctx.translate(x, y);
    ctx.rotate((rotation * Math.PI) / 180);
    ctx.globalAlpha *= element.opacity ?? 1;
    if (element.gradient?.glow) {
      ctx.shadowColor = element.gradient.glow;
      ctx.shadowBlur = element.gradient.glowBlur ?? 40;
    }
    ctx.fillStyle = element.fill || '#38bdf8';
    ctx.strokeStyle = element.stroke || element.fill || '#38bdf8';
    ctx.lineWidth = element.lineWidth ?? 4;

    if (element.shape === 'circle') {
      ctx.beginPath();
      ctx.arc(0, 0, size / 2, 0, Math.PI * 2);
      element.fill ? ctx.fill() : ctx.stroke();
    } else if (element.shape === 'triangle') {
      ctx.beginPath();
      ctx.moveTo(0, -size / 2);
      ctx.lineTo(size / 2, size / 2);
      ctx.lineTo(-size / 2, size / 2);
      ctx.closePath();
      element.fill ? ctx.fill() : ctx.stroke();
    } else if (element.shape === 'ring') {
      ctx.beginPath();
      ctx.arc(0, 0, size / 2, 0, Math.PI * 2);
      ctx.stroke();
    } else {
      const w = element.width ?? size;
      const h = element.height ?? size;
      roundRect(ctx, -w / 2, -h / 2, w, h, element.radius ?? 0);
      element.fill ? ctx.fill() : ctx.stroke();
    }
  },

  text(ctx, element, state) {
    const { designCx, designCy } = ctx.__viewport;
    // Animate from `from` to `to` when present, otherwise hold the element's own
    // x/y, which defaults to the design centre. Previously only from/to were
    // consulted, so a plain `y: 860` was silently ignored.
    const y = lerp(element.from?.y ?? element.y ?? designCy, element.to?.y ?? element.y ?? designCy, state.e);
    const x = element.x ?? lerp(element.from?.x ?? designCx, element.to?.x ?? designCx, state.e);
    const size = lerp(element.sizeFrom ?? element.size ?? 90, element.size ?? 90, state.e);
    ctx.globalAlpha *= element.opacity ?? 1;
    ctx.textAlign = element.align || 'center';
    ctx.textBaseline = 'middle';
    ctx.font = `${element.weight ?? 700} ${size}px ${element.font || 'Inter, system-ui, sans-serif'}`;
    if (element.glow) {
      ctx.shadowColor = element.glow;
      ctx.shadowBlur = element.glowBlur ?? 40;
    }
    ctx.fillStyle = element.fill || '#f8fafc';
    if (element.letterSpacing) {
      drawTracked(ctx, element.text, x, y, element.letterSpacing);
    } else {
      ctx.fillText(element.text, x, y);
    }
  },

  particles(ctx, element, localTime, scene, alpha) {
    const count = element.count ?? 90;
    const noise = rng(element.seed ?? 4242);
    const w = ctx.__viewport.designWidth;
    const h = ctx.__viewport.designHeight;
    const drift = element.drift ?? 60;
    const rise = element.rise ?? 90;
    ctx.globalAlpha *= alpha * (element.opacity ?? 0.8);
    for (let i = 0; i < count; i += 1) {
      const bx = noise.range(0, w);
      const by = noise.range(0, h);
      const phase = noise.range(0, Math.PI * 2);
      const speed = noise.range(0.2, 1);
      const radius = noise.range(element.minSize ?? 1.5, element.maxSize ?? 4.5);
      const driftPhase = noise.range(0, Math.PI * 2);
      const t = localTime * speed + phase;
      const x = bx + Math.sin(t + driftPhase) * drift;
      const y = (by - t * rise + h * 4) % h;
      const fade = clamp(1 - Math.abs(y - h / 2) / (h / 2)) * 0.8 + 0.2;
      ctx.fillStyle = rgba(element.color || '#7dd3fc', fade);
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, Math.PI * 2);
      ctx.fill();
    }
    void scene;
  },

  image(ctx, element, localTime) {
    if (!element.source) return;
    const img = ctx.__getImage(element.source);
    if (!img || !img.complete || !img.naturalWidth) return;
    const { designCx, designCy } = ctx.__viewport;
    const state = elementState(element, localTime) || { e: 1 };
    const w = (element.width ?? 640) * state.e;
    const h = (element.height ?? 360) * state.e;
    // x/y are the top-left corner in design space; without them the image is
    // centred, so the offset has to be applied after the size is known.
    const x = element.x ?? designCx - w / 2;
    const y = element.y ?? designCy - h / 2;
    ctx.globalAlpha *= element.opacity ?? 1;
    if (element.shadow) {
      ctx.shadowColor = element.shadow;
      ctx.shadowBlur = element.shadowBlur ?? 50;
    }
    if (element.radius) {
      ctx.save();
      roundRect(ctx, x, y, w, h, element.radius);
      ctx.clip();
      ctx.drawImage(img, x, y, w, h);
      ctx.restore();
    } else {
      ctx.drawImage(img, x, y, w, h);
    }
  },

  /** Canvas stand-in for a Three.js object: a shaded lit cube. */
  shape3d(ctx, element, state) {
    const { designCx, designCy } = ctx.__viewport;
    const size = (element.size ?? 160) * state.e;
    const spin = state.e * (element.spin ?? 180);
    ctx.globalAlpha *= element.opacity ?? 1;
    ctx.translate(element.x ?? designCx, element.y ?? designCy);
    ctx.rotate((spin * Math.PI) / 180);
    const front = size / 2;
    const depth = size / 2 * 0.42;
    ctx.fillStyle = element.face || '#0ea5e9';
    ctx.fillRect(-front, -front, size, size);
    ctx.fillStyle = element.side || '#075985';
    ctx.beginPath();
    ctx.moveTo(front, -front);
    ctx.lineTo(front + depth, -front + depth * 0.4);
    ctx.lineTo(front + depth, front + depth * 0.4);
    ctx.lineTo(front, front);
    ctx.closePath();
    ctx.fill();
  },
};

Renderer.ELEMENT_RENDERERS = ELEMENT_RENDERERS;


