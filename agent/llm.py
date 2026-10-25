"""Cliente mínimo de Ollama (solo biblioteca estándar)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import config


class LLMError(RuntimeError):
    pass


def chat(messages: list[dict], *, temperature: float = 0.1, model: str | None = None) -> str:
    payload = {
        "model": model or config.MODEL,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_ctx": config.NUM_CTX,
            "num_predict": config.NUM_PREDICT,
        },
    }
    req = urllib.request.Request(
        f"{config.OLLAMA_URL}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        raise LLMError(f"Ollama respondió {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise LLMError(f"No se pudo consultar a Ollama (¿el modelo sigue cargando?): {exc}") from exc
    content = (data.get("message") or {}).get("content", "")
    if not content.strip():
        raise LLMError("Ollama devolvió una respuesta vacía")
    return content


def installed_models() -> list[str]:
    """Modelos instalados en Ollama (lista vacía si no responde)."""
    try:
        with urllib.request.urlopen(f"{config.OLLAMA_URL}/api/tags", timeout=5) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return []
    # Ollama etiqueta ":latest" por defecto; se oculta para que "elite-architect" coincida con "elite-architect:latest".
    names = {m["name"].removesuffix(":latest") for m in data.get("models", []) if m.get("name")}
    return sorted(names)
