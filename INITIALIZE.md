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

La configuración mínima usa SQLite local y Ollama en `http://localhost:11434`. Revisa especialmente:

- `ASSISTANT_DATABASE_URL`: por defecto `sqlite:///./data/assistant.db`.
- `ASSISTANT_OLLAMA_URL`: dirección del servidor Ollama.
- `ASSISTANT_OLLAMA_MODEL`: modelo que Planner, Resolver y respuesta final utilizarán.
- `ASSISTANT_WORKSPACE_ROOT`: raíz permitida para analizar el proyecto local.
- `ASSISTANT_USER_*`: perfil explícito opcional que se conserva como memoria estructurada.

Antes de levantar la API, inicia Ollama y asegúrate de que el modelo configurado existe:

```powershell
ollama serve
ollama pull smtek/Qwen3.8-27B:Q3_K_M
```

Para comprobar que la instalación del paquete está sana sin depender de Ollama:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## 2. Levantar API, runtime y dashboard

Ejecuta desde la raíz del repositorio:

```powershell
.\.venv\Scripts\python.exe -m uvicorn assistant.api:app --reload
```

El proceso hace todo lo siguiente durante el startup:

1. Crea o abre la base SQLite.
2. Comprueba la disponibilidad del modelo.
3. Recupera nodos que quedaron interrumpidos.
4. Arranca el runtime persistente, secuencial y con heartbeat.
5. Expone la API y el dashboard.

Abre estas URLs:

- Dashboard: [http://127.0.0.1:8000/dashboard](http://127.0.0.1:8000/dashboard)
- Salud rápida: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)
- Datos agregados del panel: [http://127.0.0.1:8000/dashboard/data](http://127.0.0.1:8000/dashboard/data)
- Swagger: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

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

Invoke-RestMethod -Uri http://127.0.0.1:8000/projects -Method Post -ContentType "application/json" -Body $project
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
Invoke-RestMethod -Uri http://127.0.0.1:8000/tasks -Method Post -ContentType "application/json" -Body $task
```

La respuesta devuelve el `id` y el estado inicial. El runtime recogerá la tarea desde SQLite y pasará por Planner, Scheduler, Resolver, tool, Verifier y `FINAL_RESPONSE` según corresponda.

Para lanzar una auditoría repetible del proyecto:

```powershell
$projectId = "ID_DEL_PROYECTO"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/projects/$projectId/audit" -Method Post
```

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
Invoke-RestMethod "http://127.0.0.1:8000/tasks/$taskId"
Invoke-RestMethod "http://127.0.0.1:8000/tasks/$taskId/graph"
Invoke-RestMethod "http://127.0.0.1:8000/tasks/$taskId/events"
Invoke-RestMethod "http://127.0.0.1:8000/dashboard/data"
```

Si una tarea espera respuesta humana:

```powershell
$input = @{ node_id = "ID_DEL_NODO"; input = @{ answer = "continuar" } } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8000/tasks/$taskId/input" -Method Post -ContentType "application/json" -Body $input
```

Si una tarea está `BLOCKED`, aporta una solución explícita con el mismo endpoint, redefine el objetivo con `POST /tasks/{id}/redefine`, cancélala o elimínala. El runtime no adivina cómo reparar un bloqueo.

## 6. Interpretar salud y estados

`/health` y el encabezado del dashboard reflejan la disponibilidad real:

- `READY`: base, runtime y LLM disponibles.
- `DEGRADED`: el proceso está vivo, pero falta una dependencia como Ollama.
- `QUEUED`, `PLANNING`, `READY`: trabajo esperando una fase ejecutable.
- `RUNNING`, `VERIFYING`: el runtime está trabajando o comprobando un resultado.
- `WAITING`: hace falta input o aprobación humana.
- `BLOCKED`: no puede continuar sin una decisión o solución explícita.
- `SUCCEEDED`, `FAILED`, `CANCELLED`: estados terminales.

Las métricas acumuladas incluyen pasadas del worker, tareas despachadas, errores por tarea, pasadas idle, pasadas sin LLM y errores del runtime. Los colores del panel reflejan estos estados, pero el valor persistido en SQLite y los eventos son la referencia operativa.

## 7. Parar y volver a levantar

Pulsa `Ctrl+C` en la terminal de Uvicorn. La base queda intacta. Al volver a ejecutar el comando de arranque, el startup recuperará el trabajo pendiente y el dashboard volverá a leer el estado existente.

El dashboard está separado visualmente en `dashboard/`, pero se sirve desde la misma API local para conservar una sola fuente de verdad y evitar sincronizaciones frágiles.
