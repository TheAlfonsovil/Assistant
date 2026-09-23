# Assistant Core

Persistent local task engine built with Python 3.12+, SQLite, SQLAlchemy 2, Pydantic, FastAPI, asyncio and Ollama.

## Structure

The project is organized by responsibility first and by device second:

```text
startup/   load, LLM readiness, recovery
devices/   computer, mobile, home, robot
tools.py   stable action contract and dispatch
application/ / runtime.py   task graph, lifecycle and scheduler
domain/ + infrastructure/    rules and persistence
```

The computer branch is the real branch in V1. Its actions live in
`assistant/devices/computer/actions/`; mobile, home and robot are explicit
mock branches. To add an action, implement `Tool`, define its permissions and
register it in `register_actions`. To add a device, create its package and add
one `DeviceBranch` in `assistant/devices/registry.py`. See
[docs/architecture.md](docs/architecture.md) for the complete map.

The computer branch also exposes `process.start`, `process.status`,
`process.log` and `process.stop` for long-lived project processes. This is
useful for starting a local frontend, backend or development server without
blocking the task until its command timeout. Processes are tracked by the
worker using their exact PID, and stdout/stderr are written under
`data/processes/`; only processes started through this tool can be stopped.

The initial user profile is read from `ASSISTANT_USER_*` variables in `.env`
and persisted as one structured `user_profile` memory. The planner receives
that profile together with task-relevant memories. The included
`.env.example` shows the profile fields and ISO date format.

Projects are durable code resources, separate from devices and from the process working directory.
Register one with `POST /projects` using its name, absolute path, description
and audit prompt. Tasks resolve a project by explicit id or name, the default
project, or automatically when exactly one enabled project exists. A code
project can start the repeatable workflow with `POST /projects/{id}/audit`.
Auditing is read-only with respect to project files: it inventories the project,
safe configuration, dependencies, detected tests and limitations, and executes
the detected tests by default. Each audit is a normal task, so the
same project can be reviewed again without creating a permanent task.

Devices are a separate capability layer. The active computer branch represents
Windows and uses local tools; the mobile branch represents Android and reserves
an ADB transport. Their tools, interaction model and permissions can evolve
independently from code projects.

With `--fullflow`, the CLI also shows the prompt sections, prompt/context
sizes, memory and dependency counts, the validated LLM response, and elapsed
generation time. Full event payloads remain persisted for later inspection;
the terminal uses previews so a large project cannot flood the console.

## Quick start

```powershell
python -m pip install -e ".[dev]"
assistant status
assistant run
assistant run --dashboard
assistant task "revisa el proyecto y ejecuta los tests"
assistant task --fullflow "revisa el proyecto y ejecuta los tests"
uvicorn assistant.api:app --reload
```

Con la API levantada, abre el [dashboard](http://127.0.0.1:8000/dashboard) para
visualizar tareas, grafos, eventos, salud, métricas, proyectos y memoria, y para
crear o intervenir tareas desde el navegador. La guía completa de instalación,
configuración, órdenes y monitorización está en [INITIALIZE.md](INITIALIZE.md).

El dashboard incluye un resumen operativo, una tabla de tareas filtrable, detalle
de cada grafo con estados por nodo e histórico completo de transiciones,
observabilidad del modo/decisión actual, vistas separadas de dispositivos,
proyectos y memoria, y Chat-fast. Chat-fast confirma la petición inmediatamente
por SSE y sigue emitiendo el estado de la tarea persistente hasta su resultado;
si el navegador se desconecta, el runtime continúa ejecutándola y se puede
retomar desde Tareas. El control de modo idle es independiente de cancelar una
tarea: pausarlo detiene el mantenimiento automático, no borra ni cancela trabajo.

La ejecución normal muestra el resultado resumido. Para inspeccionar el ciclo completo usa `--fullflow`:

```text
[Assistant] Tarea creada | nodo=...
[Assistant] Planner: preparando el grafo
[Assistant] LLM consultado (PLANNER)
	contexto enviado: {...}
[Assistant] LLM respondió (PLANNER): propone ...
[Assistant] Scheduler: nodos listos=..., seleccionado=...
[Assistant] LLM consultado (NODE_RESOLVER)
	contexto enviado: {...}
[Assistant] LLM respondió (NODE_RESOLVER): propone OPERATION -> shell.exec
[Assistant] Ejecutando tool shell.exec
[Assistant] Resultado tool: success=True output={...}
[Assistant] Nodo verificado: SUCCESS
[Assistant] Nodo completado; el scheduler buscará el siguiente nodo elegible
[Assistant] Tarea completada
[TASK_FINISHED] id=... status=SUCCEEDED
```

El comando normal usa Ollama y aplica el presupuesto de tiempo de la tarea a Planner, Resolver, Replanner y respuesta final. Usa `--fullflow` para ver la interacción humana completa entre Planner, Resolver, tools y Verifier. Para validar el circuito sin cargar el modelo usa `assistant task --mock --fullflow "tarea de prueba"`.

`assistant run` mantiene vivo el Task Manager. La API también arranca un worker
en segundo plano durante su lifespan. Cuando no hay tareas, el worker ejecuta
una pasada de idle sin crear tareas sintéticas. El estado de la última pasada,
el número de tareas activas y el último error aparecen en `/health`. Al iniciar carga la configuración,
crea el esquema, comprueba que el LLM está listo, recupera nodos que quedaron en
ejecución y continúa con las tareas pendientes desde SQLite. Usa
`assistant run --once` para una pasada única.
`/health` también expone contadores acumulados del runtime: pasadas, tareas
despachadas, errores por tarea, pasadas idle y pasadas sin LLM disponible.

El ciclo idle solo reconcilia estado persistido y purga memoria expirada; no crea
tareas sintéticas en la configuración de producción local. Se puede pausar y
reactivar desde el dashboard, sin detener la cola de tareas. Para arrancarlo
pausado configura `ASSISTANT_IDLE_ENABLED=false`; el cambio realizado desde el
dashboard se mantiene hasta el siguiente reinicio del proceso.
Para arrancar API, runtime y dashboard en un único proceso usa
`assistant run --dashboard`; el panel queda disponible en
`http://127.0.0.1:8000/dashboard`.

Para limpiar tareas, eventos y memoria sin borrar el archivo ni el esquema de
SQLite, detén primero el runtime y sigue la sección de limpieza de
[INITIALIZE.md](INITIALIZE.md).

Cuando una tarea queda en `WAITING`, el cliente puede aportar la respuesta sin
reiniciar el flujo usando `POST /tasks/{id}/input` con `node_id` e `input`.
Las respuestas finales se generan una sola vez desde el servicio y quedan
persistidas en `task.metadata.final_response`, por lo que CLI y API comparten
el mismo resultado.

Si existen varios proyectos habilitados y ninguno es explícito o predeterminado,
la tarea queda en `WAITING` y expone una aclaración `project_selection`. El
cliente debe enviar `project_id` o `project_name` mediante el endpoint de input.
Tras esa selección la tarea vuelve a `QUEUED` y siempre atraviesa el Planner
antes de ejecutar nodos. Los nodos del planner pueden declarar
evidencia de aceptación, por ejemplo `{"exit_code": 0}`,
`{"fields": {"status": "healthy"}}`, `{"exists": ["artifact.path"]}` o
`{"contains": ["tests passed"]}`; esa evidencia se conserva en el grafo y se
comprueba antes de marcar la operación como correcta.

Una tarea `BLOCKED` no se reanuda de forma ambigua. El usuario puede aportar
una solución con el mismo endpoint de input, redefinirla completamente con
`POST /tasks/{id}/redefine`, cancelarla con `POST /tasks/{id}/cancel` o
eliminarla con `DELETE /tasks/{id}`. Redefinir conserva el identificador de la
tarea, elimina su subgrafo anterior y reinicia la planificación; eliminar
borra también nodos, edges, eventos y leases asociados. Estas operaciones
modifican la tarea, no el runtime, los prompts ni los contratos internos del
asistente.

Una verificación puede devolver `BLOCK` cuando el resultado requiere revisión
humana. En ese caso el nodo conserva su resultado y motivo, la tarea queda en
`BLOCKED` y se registra `NODE_BLOCKED`; no se reintenta ni se ejecuta una acción
generada automáticamente.

Las notificaciones usan la herramienta registrada `notify.send`. En el despliegue
local se persisten en `data/notifications.jsonl`; la entrega externa requiere un
adaptador, pero el nodo conserva el mismo lease, presupuesto, idempotencia y
verificación que cualquier otra operación. Un nodo `DECISION` evalúa una expresión
estructurada segura sin llamar de nuevo al LLM y puede cancelar ramas declaradas.

El planner puede generar nodos `OPERATION`, `SUBTASK`, `WAIT`, `VERIFY`,
`CONDITION`, `DECISION` y `NOTIFY`.
`WAIT` conserva el input enviado por el usuario y `VERIFY` valida las
dependencias completadas sin consumir otra llamada al LLM. Las operaciones se
validan antes de invocar la herramienta: el runtime comprueba que existen la
herramienta y el método, que los argumentos tienen el tipo declarado, que los
campos obligatorios están presentes y que el timeout es válido.

El planner tiene un presupuesto de `max_plan_nodes` (100 por defecto). Si una
propuesta supera ese límite, debe devolver `subtasks` y el motor persiste esas
subtareas en lugar de convertir un plan enorme en cientos de nodos. Las
dependencias pueden declarar `SUCCESS`, `FAILURE` o `ALWAYS`. Una acción fallida
entra primero en verificación: `RETRY` vuelve a `READY` con backoff; si agota
los reintentos o no es reintentable, la acción y la tarea quedan en `FAILED`.
`BLOCKED` se reserva para intervención humana, capacidad no soportada o un
grafo sin progreso. Durante la comprobación de un resultado, el estado de la
tarea también es `VERIFYING`.

Una respuesta directa, como `cuanto es 2 + 2`, se persiste como resultado final
de la tarea y se devuelve al usuario sin crear nodos de ejecución ni pedir una
segunda respuesta al LLM. Tras un reinicio, los nodos que estaban en
`RUNNING` o `VERIFYING` vuelven a `READY` junto con su tarea padre.

Para analizar estructura y relaciones del código, una operación puede usar la capability `project.analyze`. Devuelve archivos examinados, lenguajes, símbolos y aristas de imports; no se confunde con `filesystem.exists`, que solo comprueba una ruta. Para tareas de ordenador, `system.info` devuelve estado local básico y `browser.inspect` inventaría navegadores y pestañas observables. `browser.open` abre una URL pública, `browser.close_tab` cierra una pestaña identificada, `browser.close_site` cierra las pestañas observadas de un sitio y `browser.close_browser` cierra el proceso Windows identificado. Cada interacción se registra en `data/browser-interactions.jsonl` con origen, identidad, URL, objetivo y resultado. La memoria persistente vive en SQLite y sus recuerdos relevantes se incorporan al contexto del Planner.

El diseño de la siguiente fase de memoria, contextos, recovery y prefill está en
[docs/memory-context-performance.md](docs/memory-context-performance.md). Define
condicionantes débiles, expansión trazable del grafo y los límites reales de la
reutilización de KV cache con la API actual de Ollama.

Para que Chrome o Edge expongan sus pestañas y URLs, hay que iniciarlo con un puerto DevTools, por ejemplo `--remote-debugging-port=9222`. Sin ese canal, Windows solo permite identificar la ventana/proceso del navegador; el sistema informa esa limitación y no afirma conocer sus pestañas.

La salida aparece en tiempo real: `TASK_PLANNED` significa que el Planner ya respondió y creó el grafo; `LLM_CALLED` muestra la resolución del nodo; `TOOL_CALLED` y `TOOL_RESULT` delimitan la acción externa; `NODE_VERIFIED` muestra la decisión determinista; y `TASK_FINISHED` indica el estado persistido final.

La inyección de memoria está limitada por relevancia y tamaño. El perfil y los
hechos del sistema se incluyen como categorías explícitas, y cada recuerdo se
marca como datos, nunca como instrucciones. La identidad y la ruta del proyecto
proceden del registro estructurado, no de memoria libre ni de una ruta inventada
por el LLM.

The default provider is Ollama at `http://localhost:11434` using
`smtek/Qwen3.8-27B:Q3_K_M`. The local integration uses low temperature (`0.1`) and a
`32768` token context by default because Planner and Resolver responses are
schema-constrained decisions, not creative text. The included configuration uses
phase-specific thinking: Planner `medium`, Resolver `low`, Replanner `high`,
Verifier `off` and final response `low`. Change the comma-separated
`ASSISTANT_OLLAMA_REASONING_POLICY` value, or disable the feature globally with
`ASSISTANT_OLLAMA_THINKING=false`. The prompt budget reserves output tokens, so it
cannot exceed the configured context window even when
`ASSISTANT_OLLAMA_MAX_PROMPT_CHARS` is larger.
Set values in `.env` using `.env.example` as a template. Tests use mock providers
and tools, so Ollama is not required for the test suite.

See [docs/architecture.md](docs/architecture.md) and [docs/task-lifecycle.md](docs/task-lifecycle.md) for the design.
