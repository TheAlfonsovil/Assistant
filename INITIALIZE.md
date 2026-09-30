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
por ejemplo `cada 30 minutos comprueba que el servicio responde`. El
asistente crea una **tarea titular** en estado `WAITING` con
`metadata.schedule`; no es trabajo en curso, es la definición del horario. Cada
vez que vence, el runtime crea una tarea hija normal, así que hereda proyecto,
adjuntos, presupuesto, verificación y ledger, y aparece en `Tareas` como una
tarea más.

- El titular se muestra con `🔁` y el intervalo en `Tareas`.
- Los intervalos van de 60 segundos a 30 días.
- El estado se mantiene en `metadata.schedule`: `next_run_at`, `last_fired_at`,
  `runs` y `enabled`.
- Si el equipo estaba apagado se ejecuta **una** vez al volver, no una por cada
  ventana perdida.
- `SCHEDULE_CREATED`, `SCHEDULE_FIRED` y `SCHEDULE_CANCELLED` quedan en el
  histórico de eventos del titular.
- Para detenerlo, pide al asistente que cancele el horario (o `schedule.cancel`
  con el id del titular). Cancelar desactiva el horario y conserva su historial;
  no borra las ejecuciones ya realizadas.

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
