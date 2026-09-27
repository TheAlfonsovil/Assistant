# Series · generador de vídeo programático

Renderiza una animación **determinista** frame a frame en Chromium y la convierte
en MP4 con FFmpeg. Sin modelo generativo: el vídeo es código.

## 1. Requisitos

- **Node.js 18+** (probado en 24.12)
- **FFmpeg** con `libx264`. Se busca en este orden:
  1. variable de entorno `FFMPEG_PATH`
  2. `series/bin/ffmpeg.exe`
  3. `ffmpeg` en el `PATH`
- **Chromium de Playwright**: `npx playwright install chromium`

Si falta FFmpeg, el script lo dice explícitamente. Si Playwright no puede
arrancar Chromium, también.

## 2. Instalación

```bash
cd series
npm install
npx playwright install chromium
```

Si no tienes FFmpeg en el sistema, copia el binario a `series/bin/ffmpeg.exe`
(o define `FFMPEG_PATH`).

## 3. Preview

```bash
npm run dev          # http://localhost:5180
```

Controles: reproducir, pausa, reiniciar, timeline, y campos editables de FPS,
ancho, alto y formato. La lista de escenas permite saltar a cualquiera.

En el preview sí se usa `requestAnimationFrame`, pero **solo para pintar**:
el estado de la animación se calcula siempre a partir de un `time` explícito.

## 4. Exportar

```bash
npm run export       # genera output/video.mp4
npm run render       # alias
```

Proceso: levanta un servidor HTTP efímero (los ES modules están bloqueados en
`file://` por CORS), abre Chromium, fija el tiempo exacto de cada frame, extrae
el canvas con `toDataURL` y monta con FFmpeg.

## 5. Cambiar FPS, resolución y duración

Vía argumentos o editando `DEFAULT_CONFIG` en `src/config.js`:

```js
width: 854, height: 480, fps: 24, duration: 12, format: 'mp4', quality: 23
```

Las dimensiones se ajustan a números pares automáticamente (H.264 con
`yuv420p` lo exige). Para vertical: `width: 480, height: 854`.

La duración real la marcan las escenas; `config.duration` se recalcula.

## 6. Crear una escena

Una escena es **datos**, no código. Se añade en `src/scene.js`:

```js
new Scene({
  name: 'intro',
  duration: 3,
  backgroundGradient: { type: 'linear', x0: 0, y0: 0, x1: 0, y1: 1, stops: [
    { offset: 0, color: '#0b1a3a' }, { offset: 1, color: '#04060e' },
  ]},
  camera: CAMERA_PRESETS.slowPushIn(),
  elements: [
    { type: 'text', text: 'TITULO', start: 0.4, duration: 2.2, y: 860, size: 128 },
  ],
})
```

Tipos de elemento: `shape` (`rect` | `circle` | `ring` | `triangle`), `text`,
`particles`, `image`, `shape3d`.

## 7. Crear una animación

Cada elemento tiene `start`, `duration`, `delay`, `easing`, `fadeIn` y
`fadeOut`. Las funciones de interpolación viven en `src/math.js`:

`linear`, `easeIn`, `easeOut`, `easeInOut`, `smoothstep`, `easeOutBack`.

Helpers: `lerp`, `clamp`, `mapRange`, `smoothstep`, `rgba`, `mixColor`, `rng`.

## 8. Sistema de coordenadas

Las escenas se escriben en un espacio de diseño de **1920x1080** y se escalan a
la resolución de salida. La misma escena funciona a 854x480 y a 1920x1080 sin
tocar la animación.

## 9. Three.js

Canvas 2D es el renderer por defecto. `shape3d` es un sustituto que dibuja un
cubo con luz. Para 3D real hay que añadir Three.js al `index.html` y montar una
escena aparte: la arquitectura lo permite (una escena puedeceiver su propio
`render`), pero el render por Canvas es el camino por defecto.

## 10. Audio

Cada `Scene` admite un campo `audio: { start, duration, file, volume }` para
sincronizar con el timeline. La generación de audio no está implementada; el
modelo de datos está listo.

## 11. Determinismo

Es la prioridad del diseño:

- el estado visual depende **solo** de `time`;
- `requestAnimationFrame` nunca decide el estado durante la exportación;
- las partículas usan un PRNG con semilla (`rng(seed)`), nunca `Math.random()`.

```bash
node determinism.js    # compara hashes entre dos sesiones independientes
```

## Estructura

```
series/
├── index.html            # editor + canvas
├── prompts/
│   └── series_worker.md  # contrato de salida del LLM (SeriesSpec)
├── src/
│   ├── main.js           # preview y puente headless (window.series)
│   ├── config.js         # configuración y viewport
│   ├── math.js           # easing, PRNG, color
│   ├── timeline.js       # Scene, elementState, envelope
│   ├── camera.js         # cámara virtual + presets
│   ├── renderer.js       # Canvas 2D
│   ├── spec.js           # validateSpec / reviveSpec: JSON -> clases
│   └── scene.js          # escenas de la demo
├── render/
│   ├── render.js         # Chromium + FFmpeg
│   └── spec-smoke.js     # un spec JSON valida, revive y renderiza
├── output/               # frames/ y video.mp4
└── bin/ffmpeg.exe        # opcional
```

## El contrato: SeriesSpec

El motor no acepta código, acepta **datos**. Un `SeriesSpec` es un único objeto
JSON con `scenes[]`, y el worker lo produce entero en una sola respuesta
(instrucciones en `prompts/series_worker.md`).

La salida es datos por dos razones prácticas:

- Nada de lo que produce el modelo se ejecuta, así que un spec se puede
  **validar, guardar y volver a renderizar** sin volver a llamar al modelo.
- Un fallo se localiza con una ruta (`scenes[2].elements[0].type`) y se puede
  repreguntar de forma dirigida, en lugar de rehacer la tarea entera.

`src/spec.js` hace el camino de vuelta:

- `validateSpec(spec)` devuelve `{ ok, issues, totalDuration }`. Los `error`
  bloquean el render; los `warning` solo se informan.
- `reviveSpec(spec)` reconstruye `Scene` y `Camera` reales. Esto no es
  cosmético: el renderizador llama a `camera.apply()` y `scene.localTime()`, que
  son métodos, así que un objeto plano vindo de JSON falla en el primer frame.
- `specToScenes(spec)` valida y revive, y lanza con el detalle de los errores.

Desde el navegador o desde el exportador:

```js
window.series.setScenes(spec);      // valida, revive y aplica
window.series.validateScenes(spec); // valida sin aplicar
```

## Comprobaciones

```bash
npm run export            # output/video.mp4
npm run test:contract     # validación y revival del contrato (rápido, sin navegador)
npm run test:spec         # un spec JSON escrito a mano llega a píxeles
npm run test:determinism  # hashes idénticos entre dos sesiones
```

`test:contract` y `test:spec` cubren cosas distintas. Un spec que falla la
validación nunca llega a un frame, así que la cobertura del navegador no puede
detectar un error de contrato: `test:contract` comprueba que cada tipo de spec
inválido se rechaza con **la ruta exacta** (`scenes[1].duration`), que los
warnings no bloquean, y que el revival produce objetos reales. `test:spec`
comprueba que un spec válido sí produce píxeles.


## Formatos

| Formato | Códec | Notas |
|---|---|---|
| `mp4` | libx264 + yuv420p | por defecto |
| `webm` | libvpx-vp9 | sin dependencia de H.264 |
| `gif` | palettegen/paletteuse | para previews rápidos |
