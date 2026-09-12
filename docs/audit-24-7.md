# Auditoria del trabajador 24/7

## Veredicto

El proyecto ya tiene una base valida de trabajador persistente: SQLite como fuente de verdad, grafo de tareas, leases, reintentos, idempotencia, verificador determinista, memoria y cuatro ramas de dispositivo. Todavia debe considerarse un motor local en evolucion, no un trabajador 24/7 listo para operar sin supervision.

## Ya esta bien encaminado

- El arranque verifica base de datos y LLM, recupera nodos `RUNNING` y conserva tareas pendientes.
- `startup.bootstrap` unifica CLI y API.
- Las acciones del ordenador tienen un contrato comun y resultados normalizados.
- Movil, casa y robot estan aislados como mocks.
- `web` tiene timeout, limites de respuesta, solo HTTP(S), y bloquea destinos locales y privados.
- `codegraph` produce nodos de modulos/simbolos y aristas de imports con limites.
- La memoria de usuario y del sistema se actualiza de forma idempotente.
- Hay trazabilidad por eventos y pruebas de regresion.

## Riesgos prioritarios

### P0: ejecucion de comandos

`shell.exec` usa `create_subprocess_shell` y acepta texto generado por el LLM. Git tambien construye comandos concatenando argumentos. Esto permite inyeccion de shell y acciones destructivas si una propuesta del modelo, una tarea o un dato externo contiene metacaracteres.

Antes de un modo autonomo: usar ejecucion por argumentos (`create_subprocess_exec`), una allowlist por comando, directorios permitidos, confirmacion humana para escritura/eliminacion/git mutante y un modo de solo lectura por defecto.

### P0: API sin autenticacion

La API local expone crear, cancelar, reanudar y leer tareas/eventos sin autenticacion ni autorizacion. No debe publicarse fuera de localhost hasta añadir identidad, permisos por accion y proteccion de secretos.

### P1: supervisor del runtime

Una excepcion no controlada en `run_once` puede terminar `run_forever`. Falta backoff exponencial, health heartbeat, reinicio supervisado, limite de concurrencia y una politica clara para tareas bloqueadas durante dias.

### P1: aprobaciones y politica

`ToolDefinition.permissions` describe permisos pero no existe todavia un policy engine que los aplique. El sistema necesita clasificar acciones: lectura, escritura reversible, escritura destructiva, red externa y cambios de repositorio. Las clases de riesgo deben poder requerir aprobacion.

### P1: memoria

Existe almacenamiento y recuperacion, pero faltan expiracion, borrado, correccion, exportacion, consentimiento por tipo y redaccion. La memoria debe distinguir hechos, preferencias, perfil, contexto temporal y resultados de tareas; nunca debe guardar secretos por defecto.

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
