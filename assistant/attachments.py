"""Inline image attachments: validation, storage and prompt propagation.

Bytes are validated by content (magic bytes), never by the file name or the
declared MIME type. Files are stored under ``data/attachments/<task>/`` and
registered in the artifact ledger, so every later phase (orchestrator, worker,
subagent, final response) references an attachment by id instead of carrying
base64 through the event log.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

# (magic prefix, extension, canonical content type)
_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
    (b"RIFF", ".webp", "image/webp"),
)


class AttachmentError(ValueError):
    """Raised when an upload is not a supported image or exceeds the limits."""


def _sniff(data: bytes) -> tuple[str, str] | None:
    """Return ``(extension, content_type)`` for a supported image, else None."""
    for signature, extension, content_type in _SIGNATURES:
        if not data.startswith(signature):
            continue
        # RIFF is also used by wav/avi; only WEBP is an image.
        if signature == b"RIFF" and data[8:12] != b"WEBP":
            continue
        return extension, content_type
    return None


def decode_upload(
    filename: str,
    data_base64: str,
    *,
    max_bytes: int,
) -> tuple[bytes, str, str]:
    """Validate one upload and return ``(data, extension, content_type)``."""
    label = (filename or "image").strip() or "image"
    try:
        data = base64.b64decode(data_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise AttachmentError(f"{label}: invalid base64 payload") from error
    if not data:
        raise AttachmentError(f"{label}: empty payload")
    if len(data) > max_bytes:
        raise AttachmentError(f"{label}: exceeds the {max_bytes} byte limit")
    sniffed = _sniff(data)
    if sniffed is None:
        raise AttachmentError(
            f"{label}: unsupported image format (png, jpeg, gif or webp)"
        )
    return data, sniffed[0], sniffed[1]


def persist_uploads(
    root: Path,
    task_id: str,
    uploads: list[Any],
    *,
    max_bytes: int,
    max_count: int,
) -> list[dict[str, Any]]:
    """Validate and store every upload, returning prompt-safe references.

    Validation happens before anything is written, so an invalid payload fails
    the whole request instead of leaving a partially attached task.
    """
    if len(uploads) > max_count:
        raise AttachmentError(f"at most {max_count} attachments are allowed")
    decoded = []
    for upload in uploads:
        if isinstance(upload, dict):
            filename = str(upload.get("filename") or "image")
            data_base64 = str(upload.get("data_base64") or "")
        else:
            filename = str(getattr(upload, "filename", "image"))
            data_base64 = str(getattr(upload, "data_base64", ""))
        decoded.append((filename, *decode_upload(filename, data_base64, max_bytes=max_bytes)))
    directory = root / task_id
    directory.mkdir(parents=True, exist_ok=True)
    references: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for filename, data, extension, content_type in decoded:
        checksum = hashlib.sha256(data).hexdigest()
        # The same image uploaded twice in one request is one attachment, and
        # the id is scoped to the task so two tasks can hold identical bytes
        # without colliding in the immutable artifact ledger.
        existing = seen.get(checksum)
        if existing is not None:
            references.append(existing)
            continue
        path = directory / f"{uuid4().hex}{extension}"
        path.write_bytes(data)
        reference = {
            "id": hashlib.sha256(f"{task_id}:{checksum}".encode("utf-8")).hexdigest()[:32],
            "filename": filename,
            "content_type": content_type,
            "size": len(data),
            "checksum": checksum,
            "path": str(path),
        }
        seen[checksum] = reference
        references.append(reference)
    return references


def attachment_view(references: list[Any]) -> list[dict[str, Any]]:
    """Return the bounded, prompt-safe view of attachment references."""
    view: list[dict[str, Any]] = []
    for item in references or []:
        if not isinstance(item, dict):
            continue
        view.append(
            {
                "id": item.get("id"),
                "filename": item.get("filename"),
                "content_type": item.get("content_type"),
                "size": item.get("size"),
                "path": item.get("path"),
            }
        )
    return view


_EXTENSION_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def infer_content_type(path: str | None) -> str:
    """Guess an image MIME type from the file extension.

    A screen capture published by a tool may carry no explicit content type;
    without this fallback such an artifact could never become a vision part.
    """
    if not path:
        return ""
    return _EXTENSION_TYPES.get(Path(str(path)).suffix.lower(), "")


def image_parts(
    references: list[Any],
    *,
    content_type_prefix: str = "image/",
    max_bytes: int,
) -> list[dict[str, Any]]:
    """Build OpenAI-style multimodal parts for attachments that exist on disk."""
    parts: list[dict[str, Any]] = []
    for item in references or []:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        content_type = str(item.get("content_type") or "") or infer_content_type(
            str(path) if path else None
        )
        if not content_type.startswith(content_type_prefix) or not path:
            continue
        try:
            data = Path(str(path)).read_bytes()
        except OSError:
            continue
        if not data or len(data) > max_bytes:
            continue
        encoded = base64.b64encode(data).decode("ascii")
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{content_type};base64,{encoded}"},
            }
        )
    return parts
