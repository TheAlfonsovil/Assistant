# Inicializar Assistant Core y su dashboard

Guía para levantar el sistema local, enviar una orden y seguir su ejecución desde el panel.

## 1. Preparar el entorno

Desde la raíz del repositorio, abre PowerShell y crea o activa el entorno virtual:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Si PowerShell bloquea la activación, ejecuta directamente los binarios de `.venv`:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Copia la configuración de ejemplo y edita `.env`:

```powershell
Copy-Item .env.example .env
```

La configuración mínima usa SQLite local y DeepSeek-V4.1-Flash por API. Revisa especialmente:

- `ASSISTANT_DATABASE_URL`: por defecto `sqlite:///./data/assistant.db`.
- `ASSISTANT_DEEPSEEK_URL`: por defecto `https://api.deepseek.com`.
- `ASSISTANT_DEEPSEEK_MODEL`: `deepseek-flash` para DeepSeek-V4.1-Flash.
- `ASSISTANT_DEEPSEEK_API_KEY`: define la clave en el entorno del proceso; no la escribas en el `.env` versionado.
- `ASSISTANT_WORKSPACE_ROOT`: raíz permitida para analizar el proyecto local.
- `ASSISTANT_PROJECTS_ROOT`: directorio padre donde se crean los proyectos nuevos.
- `ASSISTANT_USER_*`: perfil explícito opcional que se conserva como memoria estructurada.

Antes de levantar la API, configura tu clave localmente en la terminal:

```powershell
$env:ASSISTANT_DEEPSEEK_API_KEY = '<tu-clave>'
```

Para comprobar la instalación sin consumir la API de DeepSeek:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## 2. Levantar API, runtime y dashboard

Ejecuta desde la raíz del repositorio:

```powershell
.\.venv\Scripts\python.exe -m uvicorn assistant.api:app --reload
```

Alternativamente, el comando del proyecto puede levantar API, runtime y
dashboard juntos:

```powershell
assistant run --dashboard
```

El proceso hace todo lo siguiente durante el startup:

1. Crea o abre la base SQLite.
2. Comprueba la disponibilidad del modelo.
3. Recupera nodos que quedaron interrumpidos.
4. Arranca el runtime persistente, secuencial y con heartbeat.
5. Expone la API y el dashboard.

Abre estas URLs:

- Dashboard: [http://127.0.0.1:8000/dashboard](http://127.0.0.1:8000/dashboard)
- Salud rápida: [http://127.0.0.1:8000/api/v1/health](http://127.0.0.1:8000/api/v1/health)
- Métricas agregadas: [http://127.0.0.1:8000/api/v1/metrics](http://127.0.0.1:8000/api/v1/metrics)
- Swagger: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

Todos los endpoints de la API viven bajo el prefijo `/api/v1`; `/dashboard` es la
SPA compilada (`frontend/dist`) y no forma parte de la API.

El dashboard refresca los datos cada cinco segundos. Lee directamente de SQLite a través de la API y muestra tareas, nodos, edges, eventos, proyectos, memoria, salud y métricas acumuladas del runtime. No mantiene una segunda fuente de verdad.

## 3. Registrar un proyecto

El registro permite que las órdenes de auditoría y análisis resuelvan una ruta conocida:

```powershell
$project = @{
  name = "mi-proyecto"
  path = "C:\Users\th3vil\Desktop\github\mi-proyecto"
  description = "Proyecto principal"
  project_type = "code"
  is_default = $true
} | ConvertTo-Json

Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/projects -Method Post -ContentType "application/json" -Body $project
```

También puedes registrar proyectos desde Swagger. El dashboard los incluye en el selector de nuevas órdenes.

## 4. Dar una orden

### Desde el dashboard

1. Abre `/dashboard`.
2. Pulsa `+ Nueva orden`.
3. Escribe el objetivo.
4. Selecciona un proyecto si hay más de uno habilitado.
5. Pulsa `Enviar al runtime`.
6. Selecciona la tarea creada para abrir su inspector.

### Desde PowerShell

```powershell
$task = @{ goal = "revisa el proyecto y ejecuta los tests"; project_name = "mi-proyecto"; priority = 1 } | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/tasks -Method Post -ContentType "application/json" -Body $task
```

La respuesta devuelve el `id` y el estado inicial. El runtime recogerá la tarea desde SQLite y pasará por Planner, Scheduler, Resolver, tool, Verifier y `FINAL_RESPONSE` según corresponda.

Para lanzar una auditoría repetible del proyecto:

```powershell
$projectId = "ID_DEL_PROYECTO"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/projects/$projectId/audit" -Method Post
```

La auditoría es de solo lectura y no ejecuta tests por defecto. Detecta los
tests y el comando disponible y los incluye en el resultado. Para pedir
ejecución explícita usa una orden como `audita el proyecto y ejecuta los tests`,
o crea una operación `project.audit` con `run_tests=true`. El parámetro
`run_tests` pertenece a la herramienta, no a `.env`.

La API permite el mismo control de forma explícita:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/projects/$projectId/audit?run_tests=true" -Method Post
```

### Trabajo recurrente

Pide una tarea que se repita y el asistente usará la herramienta `schedule`:
`cada 30 minutos comprueba que el servicio responde` (intervalo) o
`todos los días a las 8:00 revisa las alertas en Europe/Madrid` (hora fija).
También puedes programarlo desde la vista `Horarios` del panel.

El asistente crea una **tarea titular** en estado `WAITING` con
`metadata.schedule`; no es trabajo en curso, es la definición del horario. Cada
vez que vence, el runtime crea una tarea hija normal, así que hereda proyecto,
adjuntos, presupuesto, verificación y ledger, y aparece en `Tareas` como una
tarea más.

- Dos tipos: **intervalo** (cada N segundos, entre 60 s y 30 días) y **diario**
  (una hora local en una zona concreta). La zona por defecto es
  `ASSISTANT_SCHEDULE_TIMEZONE`; los nombres IANA necesitan el paquete
  `tzdata`, y sin él solo funcionan `UTC` y desplazamientos como `UTC+02:00`.
- En `Horarios` ves la recurrencia, la próxima ejecución en su zona local, las
  ejecuciones acumuladas y los botones `Editar`, `Pausar`/`Reanudar` y
  `Cancelar`.
- `Pausar` deja de disparar y **se puede reanudar**; al reanudar, la próxima
  ejecución se recoloca en el futuro, nunca dispara en el acto.
- `Cancelar` es **final**: el titular pasa a `CANCELLED`, deja de disparar y no
  se puede reanudar ni editar. Para volver a programarlo, crea uno nuevo. Las
  ejecuciones ya realizadas se conservan.
- Si el equipo estaba apagado se ejecuta **una** vez al volver, no una por cada
  ventana perdida; un horario diario no recupera los días que no corrió.
- `SCHEDULE_CREATED`, `SCHEDULE_FIRED`, `SCHEDULE_PAUSED`, `SCHEDULE_RESUMED`,
  `SCHEDULE_UPDATED` y `SCHEDULE_CANCELLED` quedan en el histórico de eventos
  del titular.

La configuración del runtime se carga desde `.env` cuando existe. `.env.example`
es únicamente una plantilla y no se carga automáticamente; cópiala a `.env` y
ajusta sus valores. Las variables `ASSISTANT_*` controlan el proceso local,
mientras que la memoria persistente se guarda en SQLite y no en `.env`.

## 5. Monitorizar una orden

En el dashboard:

- `Tareas actuales` muestra estado, prioridad, antigüedad e identificador.
- `Distribución` resume estados y métricas del runtime.
- `Actividad reciente` muestra el stream de eventos persistidos.
- `Proyectos y memoria` muestra el contexto disponible.
- `Task inspector` muestra nodos, estado, reintentos, errores, dependencias y eventos de la tarea.
- `+ Nueva orden` crea trabajo sin salir del panel.
- En tareas `WAITING` puedes aportar input o reanudar.
- Las tareas activas se pueden cancelar desde el inspector.

Vistas propias del panel, además de `Tareas` y `Actividad`:

- `Recursos` muestra lo que este ordenador tiene y puede hacer: nombre del host,
  núcleos, memoria, espacio libre por disco, **pantallas detectadas** (índice,
  resolución, cuál es la principal, dónde está cada una) y el escritorio virtual
  completo. El botón `Capturar` toma una captura en vivo con la misma operación
  que usaría un trabajador (`screen.capture`), y la imagen aparece con su escala y
  su origen, que es lo que permite convertir píxeles de la imagen en coordenadas
  absolutas de pantalla. Si `ratón/teclado` aparece desactivado, es que
  `ASSISTANT_ENABLE_INPUT_CONTROL` está en `false`: es opt-in porque `input.*`
  puede escribir en cualquier ventana.
- `Horarios` es el trabajo recurrente: intervalo o hora diaria, con `Pausar`
  (reversible) y `Cancelar` (final). Ver §4.
- `Métricas` incluye un bloque `Caché de contexto` con el porcentaje de acierto,
  los tokens cacheados y, si `ASSISTANT_MODEL_PRICING` está configurado, el ahorro
  que ha producido la caché. El acierto depende del **prefijo idéntico** de cada
  prompt: por eso las plantillas ponen instrucciones, catálogo de herramientas y
  esquema de salida delante, y la evidencia y las observaciones al final. Si
  editas una plantilla, `python scripts/reorder_prompts.py --check` te dice si el
  orden se ha roto y `python scripts/measure_prompt_cache.py` cuánto prefijo
  idéntico queda.

Cómo se verifica el trabajo de interfaz (el "bucle de observación"): cuando una
operación cambia lo que se ve (`browser.click`, `browser.type`,
`browser.navigate`, `input.*`), el runtime **vuelve a mirar** por su cuenta,
compara un `digest` antes y después y le pasa el resultado al siguiente turno.

- `browser.snapshot` devuelve la página como texto con una referencia por
  elemento, así que se actúa por referencia (`{"ref": "e4"}`) en vez de adivinar
  coordenadas.
- Si el digest no cambia, la operación **falla**: un clic que no cambió nada no es
  una prueba de nada. El evento `SURFACE_OBSERVED` lo deja registrado y el
  contador `attempts_without_change` avisa al modelo para que no repita la misma
  acción.
- `browser.wait_for` espera un predicado determinista (`{"text_contains": "Guardado"}`)
  en lugar de suponer que una página lenta ha terminado.
- Para ventanas que no se pueden depurar queda `screen.capture` + `input.*` con
  coordenadas derivadas de la captura, y hay que volver a capturar para verificar.

Índice estructural y presupuesto de contexto:

- El `codegraph` (módulos, símbolos y aristas) es material de orientación, no una
  copia del código: se reconstruye **solo si el árbol cambió**. La comprobación es
  una huella por ruta+tamaño+mtime, así que un proyecto grande que no se ha tocado
  cuesta milisegundos en lugar de un análisis completo, y la versión del índice no
  cambia (menos ruido en el prompt y en la caché). Ajustable con
  `ASSISTANT_CODEGRAPH_REFRESH_SECONDS` (`0` = reanalizar siempre) y
  `ASSISTANT_CODEGRAPH_MAX_FILES`, `ASSISTANT_CODEGRAPH_MAX_SYMBOLS`,
  `ASSISTANT_CODEGRAPH_MAX_EDGES`. El botón/endpoint de refresco manual fuerza el
  rebuild; el refresco automático no bloquea la tarea si falla: se registra y se
  sigue sin índice.
- Si el índice se recorta por los topes, el prompt **lo dice** (`partial`, con
  `files`/`symbols`/`edges` y el total real). Un índice recortado nunca se presenta
  como completo: leer "no hay arista" donde la arista no se guardó es peor que no
  tener índice.
- `ASSISTANT_AGENT_CONTEXT_CHARS` es el presupuesto de evidencia por turno de
  agente/orquestador (200000 por defecto, por debajo del tope duro
  `ASSISTANT_DEEPSEEK_MAX_PROMPT_CHARS`, que son 400000 ≈ 100k tokens). Si un
  contexto cabe, **no se recorta nada**. Si no cabe, el orden de degradación es
  fijo: primero evidencia y observaciones volátiles, después memoria/proyecto, y
  el catálogo de herramientas solo se recorta en detalle —nunca se sustituye por
  herramientas inventadas ni se elimina en silencio—. El último recurso lo trunca
  de forma explícita y avisa de que faltan herramientas.

Presupuestos de tarea (lo que de verdad corta el trabajo a medias):

- Cada tarea lleva sus topes y el ledger los aplica uno a uno; al agotarse uno, la
  tarea pasa a `BLOCKED` y se registra `BUDGET_EXHAUSTED` con el nombre del
  presupuesto. Los límites por defecto del motor son deliberadamente
  conservadores, así que el despliegue fija los reales en `.env`:
  `ASSISTANT_TASK_MAX_LLM_CALLS` (60), `ASSISTANT_TASK_MAX_TOOL_CALLS` (150),
  `ASSISTANT_TASK_MAX_CODEGRAPH_QUERIES` (200), `ASSISTANT_TASK_MAX_PROJECT_READS`
  (200), `ASSISTANT_TASK_MAX_SOURCE_BYTES` (60 MB), `ASSISTANT_TASK_MAX_PLAN_NODES`
  (200, que en modo agente también son los pasos máximos),
  `ASSISTANT_TASK_MAX_RETRIES` (3) y `ASSISTANT_TASK_MAX_RECOVERY_ATTEMPTS` (3).
  Subirlos cuesta tokens; bajarlos corta tareas útiles, y por eso son configuración.
- Ojo con `ASSISTANT_TASK_MAX_SOURCE_BYTES`: es el total de bytes de código que una
  tarea puede leer (`project.read`), no el tamaño máximo de un archivo. Ese límite
  aparte existe: leer un fichero **entero** de más de 200 000 bytes se rechaza, para
  que un bundle minificado no caiga de golpe en el contexto. Lo que sí se puede es
  pedir un rango con `start_line`/`end_line` aunque el fichero sea enorme, que es el
  camino que abren `types.symbols` y el `codegraph`: esquema, símbolo, rango.

Preguntas semánticas sobre el código (`types`):

- El `codegraph` dice **qué existe**; `types` dice **qué es**, dónde se define,
  quién lo usa, cómo se renombra sin dejar restos y qué errores de tipo hay. Pregunta
  a un servidor de lenguaje real (pyright) y responde con la firma y el tipo
  inferidos, no con una suposición del texto: `types.probe`, `types.hover`,
  `types.definition`, `types.references`, `types.rename`, `types.diagnostics`.
- `types.rename` es lo que una búsqueda de texto no puede justificar: el servidor
  resuelve el símbolo, así que el plan de edición es completo por construcción.
  Devuelve el plan (ficheros, líneas, número de ediciones) y solo aplica si se lo
  pides con `apply: true`; si el informe se recortó, **no lo aplica** — medio
  renombrado es peor que ninguno.
- `types.diagnostics` ejecuta el comprobador de tipos en lote (1,1 s llamando a
  `node` directamente, frente a 18,5 s con `npx`) y devuelve los errores reales
  como evidencia: severidad, fichero, línea y mensaje. Es la prueba que el
  verificador no puede fabricar por su cuenta.
- Se localiza el símbolo **por nombre** dentro del archivo
  (`{"path": "assistant/context.py", "symbol": "_codegraph_summary"}`), así que no
  hay que calcular columnas, y se informa de cuántas veces aparece ese nombre para
  que una respuesta ambigua sea visible.
- Necesita Node y pyright. `types.probe` dice si están, con las rutas resueltas y
  la versión; si no lo están devuelve el motivo exacto (`missing-node`,
  `missing-server`) y qué instalar, en lugar de inventar una respuesta. Para
  instalarlo una vez: `npx --yes pyright --version`. Si prefieres una ruta fija,
  `ASSISTANT_PYRIGHT_LANGSERVER=C:\ruta\a\langserver.index.js`.

Escalera de recuperación de código (gastar poco para saber mucho):

- `types.symbols` devuelve el **esquema de un fichero** (nombre, tipo, línea y
  profundidad) en lugar de leerlo entero. Medido en este repositorio: `service.py`
  pasa de **213 371 caracteres** (fichero completo) a **4 418** (60 símbolos de
  nivel 0), unas 48 veces menos. Si el fichero es grande, el recorte **prefiere lo
  superficial** (clases y funciones antes que la primera andanada de constantes),
  porque eso es el índice que sirve.
- El orden que conviene seguir: esquema del fichero → `types.hover` o
  `types.definition` del símbolo concreto → leer solo esas líneas. El `codegraph`
  sigue siendo lo correcto para "qué hay en el proyecto"; `types` es para
  precisión dentro de un fichero.
- No hay búsqueda de símbolos por nombre en todo el proyecto (`workspace/symbol`)
  porque pyright solo indexa lo que está abierto y devolvía vacío. Para eso está
  el `codegraph`, que ya indexa el árbol completo. Mejor no ofrecer una
  herramienta que contesta a medias que ofrecerla.

Ventanas de escritorio (`window`):

- `window.list` enumera las ventanas de nivel superior con título, clase, proceso y
  rectángulo; `window.snapshot` baja al árbol de controles de una de ellas
  (nombre, tipo, estado, rectángulo y `ref`). Es el equivalente a
  `browser.snapshot` para aplicaciones nativas, donde no hay CDP que consultar.
- Es **solo observación**: actuar sigue siendo `input.*`, que es opt-in. El
  rectángulo viene en píxeles absolutos, así que el centro se calcula sin adivinar.
- Los `ref` solo valen dentro de la respuesta que los produjo: cada instantánea es
  un estado, no un identificador estable. El árbol viene podado (se conserva lo que
  tiene nombre, lo que se puede accionar o lo que es estructural); los panes
  anónimos de relleno se descartan, que es la mitad del ruido en un árbol UIA.
- Necesita `pip install "uiautomation>=2.0"` (o `pip install -e .[uia]`). Si no
  está, `window.probe` responde `missing-uiautomation` con el motivo.

Estado real en ejecución (`debug`):

- `debug.trace` arranca un programa bajo el depurador, se para en los puntos de
  ruptura que indiques y devuelve, por cada parada, la función, el fichero, la
  línea y **los valores locales** de ese instante, más la salida del programa.
  Es lo que evita convertir el código en un bosque de `print` para averiguar qué
  valía una variable en la segunda iteración.
- El depurador es opcional: `pip install "debugpy>=1.8"` (o `pip install -e .[debug]`).
  Si no está, `debug.probe` responde `missing-debugpy` con el motivo y ninguna
  tarea falla por ello.
- No mantiene una sesión interactiva abierta entre turnos: un agente que no ve la
  pantalla solo adivinaría dónde pisar. Se ejecuta, se recoge la evidencia y se
  cierra.

Tareas que esperan tu decisión:
- Una tarea que necesita intervención humana queda en `WAITING` y **no bloquea la
  cola**: el runtime sigue despachando las demás (verificado en `runtime.py`: los
  estados activos excluyen `WAITING` y `BLOCKED`).
- Lo que ahora añade el panel es enterarte: `Resumen` muestra un bloque
  `Esperando tu respuesta (N)` con la tarea, la pregunta, las opciones (si es una
  elección de proyecto) y el endpoint exacto para responder
  (`POST /api/v1/tasks/{id}/input`). Los horarios recurrentes también viven en
  `WAITING` por diseño y se excluyen: un temporizador no es una pregunta.

Qué puede **ver** el sistema (y qué no):

- **Sí hay visión, y está activada.** `deepseek-flash` (DeepSeek-V4.1-Flash) acepta
  imágenes; `deepseek-v4-pro` no. El transporte multimodal está implementado
  (imágenes en base64 como partes `image_url`, solo en mensajes de usuario, con
  topes de imágenes y bytes) y las capturas publicadas por `screen.capture` viajan
  al siguiente turno, así que el trabajador **ve** el resultado de su propio clic
  en lugar de deducirlo.
- Comprobación directa, sin mocks: `python scripts/check_vision.py` genera un PNG
  con un patrón conocido, lo envía por el mismo camino que usa el asistente y
  verifica que la respuesta coincide con lo que hay dibujado. Si el modelo no
  leyera imágenes, ese script lo dice (falla o no coincide).
- Ajustes: `ASSISTANT_DEEPSEEK_SUPPORTS_VISION` (ponlo en `true` **solo** para un
  modelo que acepte imágenes), `ASSISTANT_VISION_MAX_IMAGES` (2 por defecto:
  adjunto + captura fresca), `ASSISTANT_VISION_DETAIL` (`low` reescala a 512×512
  antes de inferir: más rápido y barato; `original` mantiene la imagen, que es lo
  que hace falta para leer texto de una captura).
- Límites del proveedor: JPEG/PNG/GIF/WebP (detecta el contenido, no la
  extensión), 32 MiB por imagen en base64, 48 MiB por cuerpo de petición, hasta
  600 imágenes. Coste: cada imagen se reescala a ~1300×1300 equivalentes y
  cuesta como máximo 1024 tokens, independiente de su tamaño original.
- Con `ASSISTANT_DEEPSEEK_SUPPORTS_VISION=false` el sistema **no** miente: trata
  las capturas como geometría (origen y escala) y prohíbe al modelo describir lo
  que no ve. El panel `Recursos → Percepción` publica en qué estado está.

Para inspección programática:

```powershell
$taskId = "ID_DE_LA_TAREA"
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/tasks/$taskId"
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/tasks/$taskId/graph"
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/tasks/$taskId/events"
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/metrics"
```

Si una tarea espera respuesta humana:

```powershell
$input = @{ node_id = "ID_DEL_NODO"; input = @{ answer = "continuar" } } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/tasks/$taskId/input" -Method Post -ContentType "application/json" -Body $input
```

Si una tarea está `BLOCKED`, aporta una solución explícita con el mismo endpoint, redefine el objetivo con `POST /tasks/{id}/redefine`, cancélala o elimínala. El runtime no adivina cómo reparar un bloqueo.

## 6. Interpretar salud y estados

`/health` y el encabezado del dashboard reflejan la disponibilidad real:

- `READY`: base, runtime y LLM disponibles.
- `DEGRADED`: el proceso está vivo, pero falta una dependencia o la clave de DeepSeek.
- `QUEUED`, `PLANNING`, `READY`: trabajo esperando una fase ejecutable.
- `RUNNING`, `VERIFYING`: el runtime está trabajando o comprobando un resultado.
- `WAITING`: hace falta input o aprobación humana.
- `BLOCKED`: no puede continuar sin una decisión o solución explícita.
- `SUCCEEDED`, `FAILED`, `CANCELLED`: estados terminales.

Las métricas acumuladas incluyen pasadas del worker, tareas despachadas, errores por tarea, pasadas idle, pasadas sin LLM y errores del runtime. Los colores del panel reflejan estos estados, pero el valor persistido en SQLite y los eventos son la referencia operativa.

## 7. Parar y volver a levantar

Pulsa `Ctrl+C` en la terminal de Uvicorn. La base queda intacta. Al volver a ejecutar el comando de arranque, el startup recuperará el trabajo pendiente y el dashboard volverá a leer el estado existente.

El panel es una SPA en `frontend/` que se compila a `frontend/dist` y se sirve
desde la misma API local bajo `/dashboard`, para conservar una sola fuente de
verdad y evitar sincronizaciones frágiles. La vista inicial es `Resumen`;
`Tareas`, `Actividad`, `Recursos`, `Métricas` y `Chat` son módulos independientes.

## 8. Limpiar el estado sin borrar la base

Detén primero el runtime y ejecuta desde la raíz del repositorio. Este comando
conserva el archivo SQLite, el esquema y los proyectos registrados, pero elimina
tareas, grafos, eventos, leases, resultados y memorias:

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('data/assistant.db'); c.execute('PRAGMA foreign_keys=ON'); c.executescript('BEGIN; DELETE FROM node_leases; DELETE FROM task_events; DELETE FROM graph_edges; DELETE FROM task_nodes; DELETE FROM operation_results; DELETE FROM tasks; DELETE FROM memories; DELETE FROM worker_heartbeat; COMMIT;'); c.close(); print('Estado limpiado; base conservada')"
```

Al arrancar de nuevo, `ASSISTANT_COLLECT_SYSTEM_FACTS=true` y
`ASSISTANT_PERSIST_USER_PROFILE=true` volverán a crear las memorias de sistema y
el perfil definido por `ASSISTANT_USER_*`. Para mantener la memoria vacía,
desactiva temporalmente esas dos opciones en `.env`. El botón `Restablecer estado`
ejecuta el mismo reset completo desde el dashboard; también está disponible como
`POST /runtime/reset` y conserva `POST /memory/reset` por compatibilidad.
