from __future__ import annotations

import json
from datetime import date, datetime
from enum import Enum
from typing import Any

MAX_PREVIEW = 1200


def compact(value: Any, limit: int = MAX_PREVIEW) -> Any:
    """Keep event payloads useful without persisting unbounded model/tool output."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): compact(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [compact(item, limit) for item in value[:30]]
    if isinstance(value, tuple):
        return [compact(item, limit) for item in value[:30]]
    if isinstance(value, str) and len(value) > limit:
        return f"{value[:limit]}... [truncated]"
    return value


def preview(value: Any, limit: int = MAX_PREVIEW) -> str:
    return json.dumps(compact(value, limit), ensure_ascii=False, default=str)
