# Assistant Core

Persistent local task engine built with Python 3.12+, SQLite, SQLAlchemy 2, Pydantic, FastAPI, asyncio and Ollama.

## Quick start

```powershell
python -m pip install -e ".[dev]"
assistant status
assistant run
assistant task "revisa el proyecto y ejecuta los tests"
assistant task --fullflow "revisa el proyecto y ejecuta los tests"
uvicorn assistant.api:app --reload
```

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

El comando normal usa Ollama y espera sin límite de tiempo mientras el modelo genera una respuesta local. Usa `--fullflow` para ver la interacción humana completa entre Planner, Resolver, tools y Verifier. Para validar el circuito sin cargar el modelo usa `assistant task --mock --fullflow "tarea de prueba"`.

`assistant run` mantiene vivo el Task Manager: recupera tareas pendientes desde SQLite y las procesa en segundo plano. Usa `assistant run --once` para una pasada única. Cuando no hay trabajo, espera; no crea tareas infinitas de mantenimiento.

Para analizar estructura y relaciones del código, una operación puede usar la capability `project.analyze`. Devuelve archivos examinados, lenguajes, símbolos y aristas de imports; no se confunde con `filesystem.exists`, que solo comprueba una ruta. La memoria persistente vive en SQLite y sus recuerdos relevantes se incorporan al contexto del Planner.

La salida aparece en tiempo real: `TASK_PLANNED` significa que el Planner ya respondió y creó el grafo; `LLM_CALLED` muestra la resolución del nodo; `TOOL_CALLED` y `TOOL_RESULT` delimitan la acción externa; `NODE_VERIFIED` muestra la decisión determinista; y `TASK_FINISHED` indica el estado persistido final.

The default provider is Ollama at `http://localhost:11434` using `smtek/Qwen3.8-27B:Q3_K_M`. Set values in `.env` using `.env.example` as a template. Tests use mock providers and tools, so Ollama is not required for the test suite.

See [docs/architecture.md](docs/architecture.md) and [docs/task-lifecycle.md](docs/task-lifecycle.md) for the design.
