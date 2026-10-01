"""Live check: does the configured model actually read an image we send?

It builds a small PNG with a known pattern (a red field with a blue band and a
black square) through the same code path the assistant uses — ``image_parts``
plus a real chat call — and asks the model what it sees. Nothing is mocked: if
the model cannot read images, the request fails or the answer does not match the
pattern, and that is the result we want to know about.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory

from assistant.attachments import image_parts
from assistant.config import get_settings
from assistant.llm import DeepSeekLLMProvider

WIDTH, HEIGHT = 96, 96
RED = (220, 30, 30)
BLUE = (30, 60, 220)
BLACK = (0, 0, 0)


def pixel(x: int, y: int) -> tuple[int, int, int]:
    # Top band blue, middle band red, bottom band black: three easy facts.
    if y < HEIGHT // 3:
        return BLUE
    if y < 2 * HEIGHT // 3:
        return RED
    return BLACK


def png_bytes() -> bytes:
    raw = bytearray()
    for y in range(HEIGHT):
        raw.append(0)  # filter type 0
        for x in range(WIDTH):
            raw.extend(pixel(x, y))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


async def main() -> int:
    settings = get_settings()
    provider = DeepSeekLLMProvider(
        settings.deepseek_url,
        settings.deepseek_model,
        settings.deepseek_api_key,
        supports_vision=True,
        max_image_bytes=settings.attachment_max_bytes,
        max_vision_images=settings.vision_max_images,
        vision_detail=settings.vision_detail,
    )
    with TemporaryDirectory() as folder:
        image = Path(folder) / "pattern.png"
        image.write_bytes(png_bytes())
        parts = image_parts(
            [{"path": str(image), "content_type": "image/png"}],
            max_bytes=settings.attachment_max_bytes,
            detail=settings.vision_detail,
        )
        print("modelo:", settings.deepseek_model, "| detalles:", settings.vision_detail)
        print("parte generada:", parts[0]["type"], "| bytes base64:", len(parts[0]["image_url"]["url"]))
        try:
            payload = await provider.chat(
                [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Describe esta imagen en una sola frase: di de que color es "
                                    "la banda superior, la del medio y la inferior. "
                                    'Responde solo JSON: {"arriba":"...","medio":"...","abajo":"..."}'
                                ),
                            },
                            parts[0],
                        ],
                    }
                ],
                reasoning_effort="none",
                max_tokens=200,
            )
        except Exception as error:  # noqa: BLE001 - the failure itself is the answer
            print("FALLO:", type(error).__name__, str(error)[:300])
            await provider.close()
            return 1
        answer = payload["choices"][0]["message"]["content"]
        print("respuesta:", answer.strip()[:200])
        print("uso:", json.dumps(provider.last_usage))
        await provider.close()
        lowered = answer.casefold()
        ok = "azul" in lowered or "blue" in lowered
        ok = ok and ("rojo" in lowered or "red" in lowered)
        print("ve la imagen:", ok)
    return 0 if ok else 2


raise SystemExit(asyncio.run(main()))
