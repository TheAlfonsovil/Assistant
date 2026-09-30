"""Diagnóstico de la integración LLM: readiness, formato de petición y usage.

Uso:
    .venv\\Scripts\\python.exe scripts\\probe_llm.py

Comprueba, contra el endpoint configurado en `.env`:

1. si `check_ready()` reconoce el modelo configurado;
2. si el cuerpo que envía el provider (`thinking`, `reasoning_effort`,
   `response_format=json_object`) es aceptado;
3. qué campos de `usage` devuelve realmente el proveedor (tokens, caché,
   tokens de razonamiento). Un `usage` vacío no rompe la ejecución, pero
   explica por qué las métricas de tokens salen a 0.

No imprime la API key.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant.config import get_settings  # noqa: E402
from assistant.llm import DeepSeekLLMProvider  # noqa: E402


async def main() -> int:
    settings = get_settings()
    provider = DeepSeekLLMProvider(
        settings.deepseek_url,
        settings.deepseek_model,
        settings.deepseek_api_key,
        timeout=settings.deepseek_timeout,
        temperature=settings.deepseek_temperature,
        thinking=settings.deepseek_thinking,
        reasoning_policy=settings.deepseek_reasoning_policy,
        max_tokens=512,
    )
    print(f"endpoint={provider.base_url}")
    print(f"model={provider.model}")
    print(f"api_key_configured={bool(settings.deepseek_api_key)}")

    try:
        print(f"check_ready={await provider.check_ready()}")
    except Exception as error:  # noqa: BLE001 - diagnóstico
        print(f"check_ready_raised={type(error).__name__}: {error}")

    try:
        payload = await provider.chat(
            [
                {
                    "role": "system",
                    "content": "Return only a valid JSON object matching the requested output schema.",
                },
                {"role": "user", "content": 'Devuelve exactamente {"ok":true}'},
            ],
            reasoning_effort="high",
            response_format={"type": "json_object"},
            _defer_success=True,
        )
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        print(f"finish_reason={choice.get('finish_reason')}")
        print(f"content={str(message.get('content'))[:200]}")
        print(f"has_reasoning_content={bool(message.get('reasoning_content'))}")
        print(f"usage={json.dumps(payload.get('usage') or {}, ensure_ascii=False)}")
    except Exception as error:  # noqa: BLE001 - diagnóstico
        print(f"chat_raised={type(error).__name__}: {error}")
        response = getattr(error, "response", None)
        if response is not None:
            print(f"http_status={response.status_code}")
            print(f"http_body={response.text[:500]}")
    finally:
        await provider.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
