import asyncio
from typing import Annotated

import typer
from httpx import HTTPError

from .domain.models import TaskRequest
from .idle import IdleCycle
from .llm import AssistantResponse
from .observability import preview
from .runtime import TaskRuntime
from .startup.bootstrap import create_context

app = typer.Typer(help="Assistant Core CLI")


@app.command("task")
def task(
    goal: str,
    mock: Annotated[bool, typer.Option(help="Use the deterministic mock provider.")] = False,
    fullflow: Annotated[
        bool, typer.Option("--fullflow", help="Show the complete human-readable task lifecycle.")
    ] = False,
):
    async def run():
        def print_llm_trace(trace):
            if not fullflow:
                return
            phase = trace.get("phase")
            role = trace.get("role", "LLM")
            if phase == "LLM_REQUEST_BUILT":
                typer.echo(
                    f"[Assistant] LLM envia ({role}): prompt={trace['prompt_chars']} chars, "
                    f"contexto={trace['context_chars']} chars, secciones={trace['sections']}"
                )
                typer.echo(f"        prompt preview:\n{trace['prompt_preview']}")
            elif phase == "LLM_RESPONSE_PARSED":
                typer.echo(
                    f"[Assistant] LLM responde ({role}): {trace['raw_chars']} chars en "
                    f"{trace['elapsed_seconds']}s; transformado y validado como JSON"
                )
                typer.echo(f"        respuesta validada: {preview(trace['validated'], 3000)}")

        async def print_event(event):
            if not fullflow and event.event_type not in {"TASK_FAILED", "TASK_COMPLETED"}:
                return
            if fullflow and event.event_type == "LLM_CALLED":
                return
            messages = {
                "TASK_CREATED": "Tarea creada",
                "NODE_READY": "Nodo listo para ejecutar",
                "TASK_PLANNED": "Planner: preparando el grafo",
                "NODE_CREATED": f"Planner: nodo añadido ({event.payload.get('description', '')})",
                "SCHEDULER_SELECTED": f"Scheduler: nodos listos={len(event.payload.get('ready_nodes', []))}, seleccionado={event.payload.get('selected')}",
                "NODE_STARTED": f"Nodo iniciado: {event.payload.get('description', '')}",
                "LLM_REQUEST": f"LLM consultado ({event.payload.get('role', 'unknown')})",
                "LLM_RESPONSE": (
                    f"LLM respondió ({event.payload.get('role', 'unknown')}): propone "
                    f"{event.payload.get('action', event.payload.get('nodes', 'un resultado'))}"
                    + (
                        f" -> {event.payload['tool']}.{event.payload['method']}"
                        if event.payload.get("tool")
                        else ""
                    )
                ),
                "TOOL_CALLED": f"Ejecutando tool {event.payload.get('tool', '')}.{event.payload.get('method', '')}",
                "TOOL_RESULT": (
                    f"Resultado tool: success={event.payload.get('success')} "
                    f"output={preview(event.payload.get('output'))}"
                ),
                "NODE_VERIFIED": f"Nodo verificado: {event.payload.get('decision', '')}",
                "NODE_COMPLETED": "Nodo completado; el scheduler buscará el siguiente nodo elegible",
                "NODE_WAITING": f"Nodo en espera: {event.payload.get('reason', '')}",
                "NODE_BLOCKED": f"Nodo bloqueado: {event.payload.get('reason', '')}",
                "TASK_COMPLETED": "Tarea completada",
                "TASK_FAILED": f"Tarea fallida: {event.payload.get('error', '')}",
            }
            message = messages.get(event.event_type, event.event_type)
            details = f" | nodo={event.node_id}" if fullflow and event.node_id else ""
            typer.echo(f"[Assistant] {message}{details}", err=event.event_type == "TASK_FAILED")
            if fullflow and event.event_type == "LLM_REQUEST":
                context = event.payload.get("context", {})
                if isinstance(context, dict):
                    typer.echo(f"        contexto: {', '.join(context.keys())}")
                    memories = context.get("long_term_memory", context.get("relevant_memory", []))
                    typer.echo(
                        f"        memoria: {len(memories) if isinstance(memories, list) else 0} registros; "
                        f"dependencias: {len(context.get('dependency_results', []))}; "
                        f"tarea={context.get('task', {}).get('id', '-')}")
                else:
                    typer.echo("        contexto preparado")
                template = event.payload.get("prompt_template")
                if template:
                    typer.echo(
                        f"        plantilla del {event.payload.get('role', 'LLM')} "
                        f"({len(template)} caracteres):\n{template}"
                    )
            if fullflow and event.event_type == "LLM_RESPONSE":
                typer.echo(f"        propuesta validada: {preview(event.payload)}")

        context = await create_context(use_mock=mock, event_sink=print_event, trace_sink=print_llm_trace)
        service, startup = context.service, context.startup
        if fullflow:
            typer.echo(
                f"[Assistant] startup: status={startup.status} database={startup.database_ready} "
                f"llm={startup.llm_ready} recovered_nodes={startup.recovered_nodes} "
                f"unfinished_tasks={startup.unfinished_tasks} memories={len(startup.loaded_memories)}"
            )
        created = await service.create_task(TaskRequest(goal=goal))
        result = None
        try:
            result = await service.run_task(created.id)
            if result and result.runtime.final_response:
                response = AssistantResponse.model_validate(result.runtime.final_response)
                typer.echo(f"[Assistant] Respuesta ({response.response_type}): {response.title}")
                typer.echo(response.summary)
                for section, items in response.sections.items():
                    typer.echo(f"  {section}:")
                    for item in items:
                        typer.echo(f"    - {item}")
                for action in response.next_actions:
                    typer.echo(f"  Siguiente paso: {action}")
                if response.limitations:
                    typer.echo(f"  Limitaciones: {'; '.join(response.limitations)}")
        except (HTTPError, OSError, ValueError) as error:
            typer.echo(f"[TASK_FAILED] id={created.id} error={error}", err=True)
        finally:
            if result:
                typer.echo(f"[TASK_FINISHED] id={result.id} status={result.status}")
            await context.close()

    asyncio.run(run())


@app.command("status")
def status():
    typer.echo("Assistant Core ready")


@app.command("run")
def run(
    once: Annotated[bool, typer.Option("--once", help="Process queued tasks once and exit.")] = False,
    interval: Annotated[float, typer.Option(help="Seconds between background scheduler passes.")] = 5.0,
    mock: Annotated[bool, typer.Option(help="Use the deterministic mock provider.")] = False,
    dashboard: Annotated[bool, typer.Option("--dashboard", help="Start the API and dashboard instead of the CLI worker.")] = False,
):
    async def loop():
        if dashboard:
            import uvicorn

            config = uvicorn.Config("assistant.api:app", host="127.0.0.1", port=8000, reload=False)
            await uvicorn.Server(config).serve()
            return

        async def print_event(event):
            typer.echo(f"[Assistant] {event.event_type} node={event.node_id or '-'} {preview(event.payload)}")

        context = await create_context(use_mock=mock, event_sink=print_event)
        service, startup = context.service, context.startup
        typer.echo(
            f"[Assistant] startup: status={startup.status} database={startup.database_ready} "
            f"llm={startup.llm_ready} recovered_nodes={startup.recovered_nodes} "
            f"unfinished_tasks={startup.unfinished_tasks} memories={len(startup.loaded_memories)}"
        )
        runtime = TaskRuntime(
            service.repository,
            lambda task_id: service.run_task(
                task_id, wait_for_retry=False, single_step=True
            ),
            interval,
            idle_cycle=IdleCycle(
                on_idle=service.reconcile_idle,
                supervise=lambda has_work: service.reconcile_idle(has_work),
                interval=context.settings.maintenance_interval,
                supervision_interval=context.settings.maintenance_interval,
                enabled=context.settings.idle_enabled,
            ),
            is_ready=lambda: startup.llm_ready,
        )
        try:
            if once:
                count = await runtime.run_once()
                typer.echo(f"[Assistant] scheduler pass complete; active_tasks={count}")
            else:
                typer.echo(f"[Assistant] background runtime active; interval={interval}s")
                await runtime.run_forever()
        finally:
            await context.close()

    asyncio.run(loop())


if __name__ == "__main__":
    app()
