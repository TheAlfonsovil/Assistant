// Contract tests for spec.js. Pure Node: no browser, no renderer, no canvas.
// These cover what spec-smoke.js cannot, because a spec that fails validation
// never reaches a frame.
import assert from 'node:assert/strict';
import { Camera } from '../src/camera.js';
import { Scene } from '../src/timeline.js';
import { validateSpec, reviveSpec, specToScenes, normalizeSpec, SUPPORTED_ELEMENT_TYPES } from '../src/spec.js';

let passed = 0;
const results = [];
function test(name, fn) {
  try {
    fn();
    passed += 1;
    results.push(`  ok    ${name}`);
  } catch (error) {
    results.push(`  FAIL  ${name}\n        ${error.message.split('\n').join('\n        ')}`);
    process.exitCode = 1;
  }
}

/** Assert the spec is rejected, and that the reported path mentions `path`. */
function assertRejects(spec, path, label) {
  const { ok, issues } = validateSpec(spec);
  assert.equal(ok, false, `${label}: expected the spec to be rejected`);
  const errors = issues.filter((i) => i.level === 'error').map((i) => i.path);
  assert.ok(errors.includes(path), `${label}: expected an error at ${path}, got ${JSON.stringify(errors)}`);
}
function assertWarns(spec, path, label) {
  const warnings = validateSpec(spec).issues.filter((i) => i.level === 'warning').map((i) => i.path);
  assert.ok(warnings.includes(path), `${label}: expected a warning at ${path}, got ${JSON.stringify(warnings)}`);
}

const scene = (over = {}) => ({ name: 's', duration: 1, elements: [{ type: 'shape', duration: 1 }], ...over });

// --- shape of the input -----------------------------------------------------
test('rejects a non-object spec', () => assertRejects(null, '$', 'null spec'));
test('rejects a spec with no scenes', () => assertRejects({ scenes: [] }, 'scenes', 'empty scenes'));
test('rejects a spec whose scenes is not an array', () => assertRejects({ scenes: {} }, 'scenes', 'scenes object'));

test('accepts a bare array as shorthand for { scenes }', () => {
  assert.deepEqual(normalizeSpec([scene()]), { scenes: [scene()] });
  assert.equal(validateSpec([scene()]).ok, true, 'a bare array of scenes must validate');
  assert.equal(reviveSpec([scene(), scene()]).length, 2, 'and must revive');
});

// --- scene level ------------------------------------------------------------
test('rejects a non-object scene', () => assertRejects({ scenes: ['nope'] }, 'scenes[0]', 'string scene'));
test('rejects a zero duration', () => assertRejects({ scenes: [scene({ duration: 0 })] }, 'scenes[0].duration', 'zero'));
test('rejects a negative duration', () => assertRejects({ scenes: [scene({ duration: -2 })] }, 'scenes[0].duration', 'negative'));
test('rejects a non-numeric duration', () => assertRejects({ scenes: [scene({ duration: '2s' })] }, 'scenes[0].duration', 'string duration'));
test('rejects a scene with no elements', () => assertRejects({ scenes: [scene({ elements: [] })] }, 'scenes[0].elements', 'no elements'));

// --- element level ----------------------------------------------------------
test('rejects an unsupported element type', () =>
  assertRejects({ scenes: [scene({ elements: [{ type: 'hologram' }] })] }, 'scenes[0].elements[0].type', 'bad type'));
test('names the supported types in the error', () => {
  const { issues } = validateSpec({ scenes: [scene({ elements: [{ type: 'hologram' }] })] });
  const message = issues.find((i) => i.level === 'error').message;
  for (const type of SUPPORTED_ELEMENT_TYPES) assert.ok(message.includes(type), `error should list "${type}"`);
});
test('rejects a text element with no text', () =>
  assertRejects({ scenes: [scene({ elements: [{ type: 'text', duration: 1 }] })] }, 'scenes[0].elements[0].text', 'empty text'));
test('rejects a negative element duration', () =>
  assertRejects({ scenes: [scene({ elements: [{ type: 'shape', duration: -1 }] })] }, 'scenes[0].elements[0].duration', 'negative'));
test('rejects a non-numeric coordinate', () =>
  assertRejects({ scenes: [scene({ elements: [{ type: 'shape', x: 'left' }] })] }, 'scenes[0].elements[0].x', 'string x'));

// --- warnings must not block ------------------------------------------------
test('warns but does not block on an unknown easing', () => {
  const spec = { scenes: [scene({ elements: [{ type: 'shape', easing: 'bouncey' }] })] };
  assert.equal(validateSpec(spec).ok, true, 'an unknown easing degrades to linear, it does not invalidate the spec');
  assertWarns(spec, 'scenes[0].elements[0].easing', 'unknown easing');
});
test('warns about unseeded particles', () => {
  const spec = { scenes: [scene({ elements: [{ type: 'particles', count: 10 }] })] };
  assert.equal(validateSpec(spec).ok, true, 'unseeded particles are non-reproducible but still renderable');
  assertWarns(spec, 'scenes[0].elements[0].seed', 'unseeded particles');
});
test('warns about a coordinate far outside the frame', () => {
  const spec = { scenes: [scene({ elements: [{ type: 'shape', x: 9000 }] })] };
  assert.equal(validateSpec(spec).ok, true, 'an off-frame element is suspicious, not invalid');
  assertWarns(spec, 'scenes[0].elements[0].x', 'off-frame x');
});
test('does not warn about a deliberate overshoot', () => {
  const { issues } = validateSpec({ scenes: [scene({ elements: [{ type: 'shape', x: 2400 }] })] });
  assert.ok(!issues.some((i) => i.path === 'scenes[0].elements[0].x'), '1.5x overshoot is a normal animation');
});
test('warns about a very long total duration', () => {
  const spec = { scenes: [scene({ duration: 95 })] };
  assert.equal(validateSpec(spec).ok, true, 'a long spec still renders, just slowly');
  assertWarns(spec, 'scenes', 'long series');

// --- camera -----------------------------------------------------------------
test('rejects a zero zoom', () =>
  assertRejects({ scenes: [scene({ camera: { zoom: 0 } })] }, 'scenes[0].camera.zoom', 'zero zoom'));
test('rejects a negative zoom', () =>
  assertRejects({ scenes: [scene({ camera: { zoom: -1 } })] }, 'scenes[0].camera.zoom', 'negative zoom'));
test('rejects non-array keyframes', () =>
  assertRejects({ scenes: [scene({ camera: { keyframes: 'slow' } })] }, 'scenes[0].camera.keyframes', 'string keyframes'));

// --- revival ----------------------------------------------------------------
test('revives scenes into real Scene instances', () => {
  const [first] = reviveSpec({ scenes: [scene()] });
  assert.ok(first instanceof Scene, 'the renderer calls scene.localTime(), so it must be a Scene');
  assert.equal(typeof first.localTime, 'function');
});
test('revives a static camera into a real Camera with defaults', () => {
  const [revived] = reviveSpec({ scenes: [scene({ camera: {} })] });
  assert.ok(revived.camera instanceof Camera, 'a plain object would throw on the first frame');
  assert.equal(revived.camera.x, 960, 'default centre x');
  assert.equal(revived.camera.y, 540, 'default centre y');
  assert.equal(revived.camera.zoom, 1);
  const state = revived.camera.stateAt(1);
  assert.equal(typeof state.zoom, 'number', 'a camera with no keyframes must still be a function of time');
  assert.ok(Number.isFinite(state.zoom));
});
test('revives a keyed camera and interpolates between keyframes', () => {
  const [revived] = reviveSpec({
    scenes: [scene({ camera: { keyframes: [{ time: 0, zoom: 1 }, { time: 2, zoom: 2 }] } })],
  });
  assert.ok(revived.camera instanceof Camera);
  assert.equal(revived.camera.stateAt(0).zoom, 1);
  assert.equal(revived.camera.stateAt(2).zoom, 2);
  assert.ok(revived.camera.stateAt(1).zoom > 1 && revived.camera.stateAt(1).zoom < 2, 'midpoint must interpolate');
});
test('a scene without a camera revives to null, not a broken object', () => {
  const [revived] = reviveSpec({ scenes: [scene()] });
  assert.equal(revived.camera, null);
});
test('layouts scenes back to back regardless of any start in the JSON', () => {
  const scenes = reviveSpec({ scenes: [scene({ start: 99, duration: 2 }), scene({ duration: 3 })] });
  assert.equal(scenes[0].start, 0, 'a start smuggled in through JSON must not break the timeline');
  assert.equal(scenes[1].start, 2);
});
test('applies Scene defaults for a sparse scene', () => {
  const [revived] = reviveSpec({ scenes: [{ elements: [{ type: 'shape' }] }] });
  assert.equal(revived.duration, 3, 'duration defaults to 3s');
  assert.deepEqual(revived.transitionIn, { type: 'fade', duration: 0.6 });
});

// --- specToScenes ----------------------------------------------------------
test('specToScenes reports the total duration', () => {
  const { scenes, totalDuration, issues } = specToScenes({ scenes: [scene({ duration: 2 }), scene({ duration: 1.5 })] });
  assert.equal(scenes.length, 2);
  assert.equal(totalDuration, 3.5);
  assert.ok(!issues.some((i) => i.level === 'error'));
});
test('specToScenes throws and names the failing path', () => {
  assert.throws(
    () => specToScenes({ scenes: [scene(), scene({ duration: -1 })] }),
    (error) => {
      assert.match(error.message, /invalid series spec/);
      assert.match(error.message, /scenes\[1\]\.duration/, 'the error must point at the exact path');
      return true;
    },
  );
});

console.log('spec contract tests\n');
console.log(results.join('\n'));
console.log(`\n${passed}/${results.length} passed`);
if (process.exitCode) console.log('FAILED');

});
