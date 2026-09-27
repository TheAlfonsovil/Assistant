# Series worker

Generates a **SeriesSpec**: a single JSON object describing an entire short
sequence, from concept to final frame.

## Why one prompt

The whole spec is emitted in one response, as JSON. Three consequences:

- There is no multi-step state to reconcile. A pipeline of planners tends to
  drift: scene 6 forgets the palette that scene 1 established. One prompt keeps
  the whole thing in context and consistent.
- The output is **data, not code**. Nothing here can execute, and a spec can be
  validated, diffed, stored and re-rendered without running the model again.
- It is retryable. Validation errors come back with a `path`, so the same prompt
  can be re-asked with a targeted fix instead of restarting the whole job.

The cost is a bigger response and a stricter format. Both are acceptable: a bad
spec is caught by validation for free, whereas a bad code artifact is caught by
rendering it.

## Contract

Respond with **one JSON object and nothing else**. No prose, no markdown fence.
The renderer consumes this shape directly.

```json
{
  "title": "string",
  "synopsis": "string",
  "style": { "palette": ["#hex", "#hex"], "mood": "string" },
  "scenes": [ ... ]
}
```

### Scene

```json
{
  "name": "string",
  "description": "string",
  "duration": 3.0,
  "background": "#hex",
  "backgroundGradient": {
    "type": "linear",
    "x0": 0, "y0": 0, "x1": 1, "y1": 1,
    "stops": [{ "offset": 0, "color": "#hex" }, { "offset": 1, "color": "#hex" }]
  },
  "transitionIn":  { "type": "fade", "duration": 0.6 },
  "transitionOut": { "type": "fade", "duration": 0.6 },
  "camera": { "x": 960, "y": 540, "zoom": 1, "shake": 0 },
  "elements": [ ... ]
}
```

`duration` is in seconds. Omit `start`: scenes are laid out back to back.

### Element

Every element has `type`, `start` and `duration`, both relative to **its own
scene**, not the whole video.

| type | purpose | key fields |
| --- | --- | --- |
| `text` | titles, captions | `text`, `x`, `y`, `size`, `fill`, `align`, `letterSpacing` |
| `shape` | geometry | `shape` (`rect`/`circle`/`ring`/`triangle`), `x`, `y`, `size`, `fill`, `stroke`, `lineWidth` |
| `particles` | atmosphere | `count`, `seed`, `color`, `minSize`, `maxSize`, `drift`, `rise` |
| `image` | artwork | `source` (URL or data URI), `x`, `y`, `width`, `height`, `radius` |
| `shape3d` | solid volume | `size`, `spin`, `face`, `side` |

Optional on any element: `from`/`to` (`{x, y}`) to animate position,
`sizeFrom`/`size` to animate scale, `opacity`, `easing`.

### Easing

`linear`, `easeIn`, `easeOut`, `easeInOut`, `smoothstep`, `easeOutBack`.
An unknown name behaves as `linear` and raises a warning.

### Camera

`x` and `y` are absolute design positions; the default `960, 540` is the frame
centre. `zoom` of `1` is the base scale, `1.2` is 20% closer. `shake` is an
amplitude in design units.

For a move that changes over the scene, use `keyframes`:

```json
"camera": {
  "keyframes": [
    { "time": 0.0, "x": 1180, "zoom": 1.00, "easing": "easeInOut" },
    { "time": 3.5, "x": 760,  "zoom": 1.12 }
  ]
}
```

## Coordinates

Everything is authored in a **1920x1080 design space**, regardless of the output
resolution. The renderer maps it onto the real frame, so a spec renders
identically at 854x480 and 1920x1080.

- `x`: 0 is the left edge, 1920 the right.
- `y`: 0 is the top edge, 1080 the bottom.
- The centre is `960, 540`. Leaving `x`/`y` off centres the element.
- Stay within `0..1920` / `0..1080`. Deliberate overshoot is fine; a title at
  `x: 2400` is a bug.

Keep a title inside roughly `200..1720` horizontally, and avoid the outer `60`
units vertically, so nothing is clipped when the camera pushes in.

## Rules

1. **Keep the palette tight.** Three to five hex values, reused across every
   scene. Re-declaring the background per scene is expected; inventing a new
   colour in the last scene is not.
2. **Seed every particle system.** An unseeded system is not reproducible, and
   the same spec will not render identically twice. Pick an explicit integer.
3. **One idea per scene.** A scene is a beat, not a sequence. If a scene needs
   more than about six elements, it is really two scenes.
4. **Vary the camera.** Identical framing across consecutive scenes reads as a
   slideshow. Alternate pushes, pans and static shots.
5. **Overlap transitions.** The last element of a scene should end within about
   `0.3s` of the scene duration, and the first of the next should start at `0`.
   That crossfade is what hides the cut.
6. **Total duration** should land between `8` and `30` seconds unless asked
   otherwise. Rendering is CPU-bound and costs time linearly.
7. **Do not invent element types.** Anything outside the table above fails
   validation and the render is rejected.

## Determinism

The renderer is a pure function of `time`. Do not ask for anything that implies
a random or wall-clock source. `particles` provides randomness, but only through
its `seed`.

## Failure handling

If a spec is rejected, the error names a path, for example:

```
error: scenes[2].elements[0].type: unsupported type "waveform"; supported: shape, text, particles, image, shape3d
```

Fix that path and nothing else. Do not rewrite scenes that validated.
