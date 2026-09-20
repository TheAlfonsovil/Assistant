# Auditoria del trabajador 24/7

## Veredicto

El proyecto ya tiene una base valida de trabajador persistente: SQLite como fuente de verdad, grafo de tareas, leases, reintentos, recuperacion de fallos, heartbeat durable, idempotencia, verificador determinista, memoria y cuatro ramas de dispositivo. Sigue siendo un worker local en evolucion: el despliegue y la supervision del proceso requieren adaptadores externos, pero el ciclo interno de tareas es durable y recuperable.

## Ya esta bien encaminado

- El arranque verifica base de datos y LLM, recupera nodos `RUNNING` y conserva tareas pendientes.
- `startup.bootstrap` unifica CLI y API.
- Las acciones del ordenador tienen un contrato comun y resultados normalizados.
- Movil, casa y robot estan aislados como mocks.
- `web` tiene timeout, limites de respuesta, solo HTTP(S), y bloquea destinos locales y privados.
- `codegraph` produce nodos de modulos/simbolos y aristas de imports con limites.
- La memoria de usuario y del sistema se actualiza de forma idempotente.
- Hay trazabilidad por eventos y pruebas de regresion.

## Ajustes de alcance de la auditoria

- Las métricas de SQLite pertenecen a **recursos y memoria**, no a observabilidad general:
  deben mostrar estado de la conexión, tamaño de la base de datos, número de tablas,
  recuento de filas por tabla, crecimiento, bloqueos y última migración aplicada.
- Las métricas de dispositivos pertenecen a **recursos > dispositivos**:
  deben mostrar disponibilidad, transporte, capacidades, latencia, errores,
  reconexiones y última actividad de cada rama.
- Las trazas entre API, planner, LLM, herramienta y verificador ya forman parte de
  la **observabilidad de la tarea**. No se deben duplicar como una carencia
  independiente; lo que falta es completar su cobertura y hacerla consultable.

## Riesgos prioritarios

### P0: ejecucion de comandos

`shell.exec` usa `create_subprocess_shell` y acepta texto generado por el LLM. Git tambien construye comandos concatenando argumentos. Esto permite inyeccion de shell y acciones destructivas si una propuesta del modelo, una tarea o un dato externo contiene metacaracteres.

Antes de un modo autonomo: usar ejecucion por argumentos (`create_subprocess_exec`), una allowlist por comando, directorios permitidos, confirmacion humana para escritura/eliminacion/git mutante y un modo de solo lectura por defecto.

### P0: API sin autenticacion

La API local expone crear, cancelar, reanudar y leer tareas/eventos sin autenticacion ni autorizacion. No debe publicarse fuera de localhost hasta añadir identidad, permisos por accion y proteccion de secretos.

### P1: supervisor externo del proceso

El runtime tiene backoff y heartbeat persistido, y aisla excepciones por tarea. Todavia falta un supervisor externo que reinicie el proceso ante terminacion del interprete, y renovacion periodica de leases para operaciones que excedan su presupuesto previsto.

### P1: aprobaciones y politica

`ToolDefinition.permissions` describe permisos pero no existe todavia un policy engine que los aplique. El sistema necesita clasificar acciones: lectura, escritura reversible, escritura destructiva, red externa y cambios de repositorio. Las clases de riesgo deben poder requerir aprobacion.

### P1: memoria

Existe almacenamiento y recuperacion, pero faltan expiracion, borrado, correccion, exportacion, consentimiento por tipo y redaccion. La memoria debe distinguir hechos, preferencias, perfil, contexto temporal y resultados de tareas; nunca debe guardar secretos por defecto.

En esta misma sección debe incorporarse un panel de recursos SQLite con:

- salud de la conexión y modo WAL;
- tamaño del fichero y espacio libre;
- tablas y recuento de filas;
- crecimiento de eventos, tareas, nodos y memoria;
- bloqueos y tiempos de escritura;
- última migración y versión de esquema;
- fecha del último backup y resultado de la última restauración verificada.

### P2: resiliencia de red y LLM

`web.fetch` no sigue redirecciones, lo cual es prudente, pero aun conviene registrar la causa y permitir una politica de redirecciones segura. El proveedor LLM necesita circuit breaker, limites de tokens, cancelacion y validacion de coste/tiempo por tarea.

### P2: codegraph

El grafo actual es muy util para Python, pero no es aun un grafo semantico completo: no resuelve imports a archivos, no modela llamadas, configuracion, SQL, frontend o dependencias de build. Puede evolucionar por analizadores de lenguaje sin cambiar el contrato de la herramienta.

## Flujo objetivo para trabajador 24/7

```text
startup -> readiness -> recover -> load memory/profile -> scheduler
   -> policy check -> plan -> approval if needed -> execute with lease
   -> verify -> persist event/result -> retry/backoff or block
   -> heartbeat/metrics -> graceful shutdown
```

## Siguiente orden recomendado

1. Policy engine y confirmaciones humanas para acciones de riesgo.
2. Reemplazar shell libre por comandos tipados y allowlist.
3. Autenticación local de API y permisos.
4. Supervisor, heartbeat, backoff y límites de concurrencia.
5. API de memoria: remember, list, forget, redact y export.
6. Tests de crash/restart, duplicación, timeout, concurrencia y recuperación.
7. Codegraph multi-lenguaje y resolución semántica.

## Conclusion

La arquitectura ya permite crecer por dispositivos y acciones sin tocar el motor. La distancia entre este prototipo sólido y un trabajador autónomo fiable no está en añadir más herramientas, sino en control operativo: permisos, aprobaciones, recuperación, supervisión, seguridad y memoria gobernable.

## Mejoras operativas para el despliegue local

- El runtime permite pausar `idle` sin detener la cola secuencial. `ASSISTANT_IDLE_ENABLED=false` lo deja pausado desde el arranque y el dashboard permite cambiarlo durante la sesión.
- La API expone el estado de idle, incluido el número de intentos omitidos por reentrada, para distinguir una pausa intencionada de saturación del ciclo de mantenimiento.
- Las respuestas de Ollama conservan `prompt_eval_count` y `eval_count` cuando el proveedor los devuelve. El dashboard muestra esos tokens medidos aparte de la estimación `caracteres / 4`.
- La recuperación de nodos interrumpidos y tareas que estaban planificando deja eventos `NODE_RECOVERED` y `TASK_RECOVERED_FROM_PLANNING`, haciendo visible el coste de un reinicio.
- El presupuesto temporal de tarea y el circuit breaker del LLM siguen siendo los límites principales para un modelo local lento. El procesamiento continúa secuencial para evitar saturar la máquina de prueba.

Estas medidas no convierten la API en un servicio seguro ni sustituyen un supervisor del sistema operativo. La API debe seguir enlazada a localhost; shell libre, dispositivos reales, auth, policy engine y concurrencia paralela quedan deliberadamente fuera de este despliegue de prueba.

## Mejoras funcionales aplicadas

- El ciclo `idle` aplica cooldown y evita reentradas, para no crear mantenimientos duplicados en pasadas consecutivas sin trabajo.
- `resume` solo reactiva un nodo `WAITING` cuando existe exactamente uno; la entrada dirigida continúa usando `submit_task_input`.
- Los argumentos se validan contra el esquema declarado por cada herramienta antes de ejecutarla.
- Las operaciones no idempotentes no reintentan automáticamente fallos retryable sin una clave de idempotencia explícita.
- El lease se amplía según el timeout de la operación antes de invocar la herramienta.
- Los fallos del replanner se persisten como `REPLAN_FAILED` y bloquean explícitamente la tarea.
- El replanner admite reintento dirigido, reinicio de tarea, ramas `FIX`, subtareas alternativas y finalización controlada.
- `NOTIFY` persiste su entrega como operación normal y `DECISION` evalúa expresiones estructuradas sin consumir una llamada al LLM.
- El preflight rechaza herramientas, métodos, argumentos obligatorios y timeouts inválidos antes de ejecutar efectos externos.
- El ciclo idle reconcilia periódicamente incluso mientras existen tareas activas y mantiene separado el mantenimiento con cooldown.
- El contexto del resolver incluye resultados completados acotados para propagar evidencia entre ramas del grafo.
- La ejecución del grafo permanece secuencial por decisión de diseño actual.
- Las herramientas largas mantienen el lease mediante renovaciones periódicas; si se pierde, el resultado no entra en verificación.
- Ollama tiene timeout de cliente, límites de prompt/respuesta y circuit breaker configurable.
- Ollama aplica thinking por fase: Planner medio, Resolver bajo, Replanner alto,
  Verifier apagado y respuesta final baja; el razonamiento no se incluye en el JSON.
- La memoria tiene expiración, purga en idle, exportación, borrado y redacción explícita.

## Recursos > dispositivos

La sección de dispositivos debe ser el lugar único para las métricas de hardware
y transportes. Para cada dispositivo o rama debe exponer disponibilidad,
capacidades anunciadas, latencia de la última operación, errores consecutivos,
reconexiones, último heartbeat y timestamp de la última actividad. Esto complementa
la observabilidad de la tarea, pero no la sustituye.

## Observabilidad de la tarea

La trazabilidad API -> planner -> LLM -> herramienta -> verificador ya está
contemplada en la observabilidad de cada tarea mediante eventos persistidos.
La mejora pendiente es cubrir todos los caminos de error, propagar un correlation
ID estable, medir cada tramo y permitir consultar la secuencia completa desde el
dashboard sin duplicar métricas en la sección de dispositivos.

## Límites de arquitectura y mantenibilidad

La separación entre política, ejecución y efectos externos debe quedar explícita:

- **API/control plane**: recibe solicitudes y expone estado; no ejecuta efectos por sí mismo.
- **Scheduler**: decide qué tarea puede avanzar y aplica prioridades, fairness y backpressure.
- **Workers de ejecución**: ejecutan operaciones con lease, timeout, cancelación e idempotencia.
- **Policy engine**: evalúa permisos, riesgo y necesidad de aprobación antes de efectos externos.
- **Adaptadores de dispositivos**: encapsulan transporte, capacidades, reconexión y health.

La extracción recomendada es gradual: primero policy engine y adaptadores,
después lease manager y execution coordinator, y finalmente scheduler y recovery
coordinator. Cada extracción debe conservar los contratos actuales y añadir pruebas
de arquitectura que impidan que API, scheduler o planner llamen directamente a
filesystem, shell, Git o transportes físicos.

La meta de mantenibilidad para producción incluye:

- ADRs para decisiones de arquitectura y seguridad;
- CI con lint, tests y análisis estático;
- contratos versionados para herramientas y dispositivos;
- runbooks operativos de recuperación, backup, restauración y pérdida de conexión;
- pruebas de límites de dependencia entre las capas anteriores.
