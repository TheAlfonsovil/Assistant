# Auto-mejora de Assistant

Assistant puede trabajar sobre su propio repositorio cuando `ASSISTANT_WORKSPACE_ROOT` apunta a la carpeta del proyecto. La auto-mejora no significa que el LLM pueda reescribirse y continuar sin control: significa ejecutar un flujo verificable de ingeniería.

## Flujo obligatorio

```text
entender objetivo
  -> codegraph/project.analyze
  -> leer solo archivos relevantes
  -> proponer cambios pequeños
  -> aplicar cambios
  -> ejecutar tests/lint
  -> revisar diff y errores
  -> informar resultado
```

Para tareas sobre este repositorio, el Planner debe crear nodos separados para análisis, implementación y validación. El cambio no se considera terminado si no hay una comprobación ejecutable. Commit y push no forman parte del flujo automático salvo petición explícita.

## Acciones puntuales

Si no existe una herramienta adecuada, el resolver puede devolver `CREATE_ACTION` con un `ActionProposal`:

- nombre;
- descripción;
- lenguaje;
- código;
- entradas;
- nivel de seguridad.

Assistant guarda el script en `data/generated_actions/`, crea un artefacto revisable y deja la tarea en `WAITING`. No ejecuta ese script automáticamente. La ejecución requiere una futura fase explícita de revisión/aprobación.

## Imposibilidad

El resolver debe usar `BLOCK` cuando la petición sea imposible, insegura, no autorizada o requiera una capacidad ausente. Debe explicar el motivo concreto. `WAIT` se reserva para aprobación del usuario, credenciales o una condición externa.

## Límites actuales

- La autenticación de API está fuera de alcance por decisión del proyecto.
- Shell y Git todavía necesitan una política de comandos tipada y una allowlist antes de confiar en autonomía completa.
- El LLM nunca debe recibir permiso implícito para hacer commit, push, borrar datos o ejecutar un script recién generado.
