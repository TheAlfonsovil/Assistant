from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ...domain.models import Project, ProjectRequest
from ..deps import get_context, get_runtime
from ..serializers import project_view

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("")
async def list_projects(request: Request) -> list:
    projects = await get_context(request).service.list_projects()
    return [project_view(project) for project in projects]


@router.post("")
async def create_project(request: Request, project_request: ProjectRequest) -> dict:
    try:
        project = await get_context(request).service.create_project(
            Project.model_validate(project_request.model_dump())
        )
    except ValueError as error:
        # A directory that already has a project is a conflict, not a server
        # error: the dashboard shows the message so the user sees which project
        # owns the path instead of a 500.
        raise HTTPException(409, str(error)) from error
    return project_view(project)


@router.get("/{project_id}")
async def get_project(request: Request, project_id: str) -> dict:
    project = await get_context(request).service.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project_view(project)


@router.put("/{project_id}")
async def update_project(request: Request, project_id: str, project_request: ProjectRequest) -> dict:
    project = Project(id=project_id, **project_request.model_dump())
    try:
        updated = await get_context(request).service.update_project(project)
    except KeyError:
        raise HTTPException(404, "Project not found") from None
    return project_view(updated)


@router.delete("/{project_id}")
async def delete_project(request: Request, project_id: str) -> dict:
    try:
        deleted = await get_context(request).service.delete_project(project_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if not deleted:
        raise HTTPException(404, "Project not found")
    return {"deleted": True, "id": project_id}


@router.post("/{project_id}/audit")
async def audit_project(request: Request, project_id: str, run_tests: bool = False) -> dict:
    task = await get_context(request).service.create_project_audit_task(
        project_id, run_tests=run_tests
    )
    if not task:
        raise HTTPException(404, "Project not found or disabled")
    get_runtime(request).wake()
    return {"id": task.id, "status": task.status, "project_id": task.project_id, "run_tests": run_tests}


@router.post("/{project_id}/codegraph/refresh")
async def refresh_project_codegraph(request: Request, project_id: str, force: bool = True) -> dict:
    """Rebuild a project's structural index. An explicit request rebuilds by default."""
    try:
        project = await get_context(request).service.refresh_project_codegraph(
            project_id, force=force
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    if not project:
        raise HTTPException(404, "Project not found")
    return project_view(project)
