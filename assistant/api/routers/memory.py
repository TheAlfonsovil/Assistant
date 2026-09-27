from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ...domain.models import MemoryRecord
from ..deps import get_context
from .runtime import perform_reset

router = APIRouter(prefix="/memory", tags=["memory"])


class MemoryWrite(BaseModel):
    kind: str = Field(default="note", min_length=1, max_length=64)
    key: str = Field(min_length=1, max_length=200)
    value: object = None
    source: str = Field(default="USER", max_length=64)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


@router.post("")
async def create_memory(request: Request, payload: MemoryWrite) -> dict:
    """Record a durable fact so workers can reuse it on later tasks."""
    memory = MemoryRecord(
        kind=payload.kind,
        key=payload.key,
        value=payload.value,
        source=payload.source,
        confidence=payload.confidence,
    )
    await get_context(request).service.repository.save_memory(memory)
    return memory.model_dump(mode="json")


@router.get("")
async def list_memory(request: Request) -> list:
    memories = await get_context(request).service.repository.list_memory()
    return [memory.model_dump(mode="json") for memory in memories]


@router.get("/export")
async def export_memory(request: Request):
    return await get_context(request).service.repository.export_memory()


@router.get("/summary")
async def memory_summary(request: Request) -> dict:
    """Row counts per SQLite table so the memory view can show DB state."""
    repository = get_context(request).service.repository
    tables = await repository.table_summary()
    return {
        "tables": tables,
        "total_rows": sum(int(table["rows"]) for table in tables),
    }


@router.post("/reset")
async def reset_memory(request: Request) -> dict:
    return await perform_reset(request)


@router.post("/purge-expired")
async def purge_expired_memory(request: Request) -> dict:
    count = await get_context(request).service.repository.purge_expired_memory()
    return {"purged": count}


@router.post("/{memory_id}/redact")
async def redact_memory(request: Request, memory_id: str) -> dict:
    memory = await get_context(request).service.repository.redact_memory(memory_id)
    if memory is None:
        raise HTTPException(404, "Memory not found")
    return memory.model_dump(mode="json")


@router.delete("/{memory_id}")
async def delete_memory(request: Request, memory_id: str) -> dict:
    if not await get_context(request).service.repository.delete_memory(memory_id):
        raise HTTPException(404, "Memory not found")
    return {"deleted": True, "id": memory_id}
