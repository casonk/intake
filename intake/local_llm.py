"""Minimal client for the local Ollama server used by intake's optional text steps.

Mail text is sent only to the configured local base URL (``translation_base_url``, default
``http://localhost:11434``), never to a cloud API.  Every failure returns ``None`` so callers can
fall back to deterministic behaviour instead of raising.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "llama3.2:3b-instruct-q8_0"


def generate(
    settings: dict,
    prompt: str,
    *,
    model: str | None = None,
    num_predict: int = 120,
    json_mode: bool = False,
    timeout: int = 45,
) -> str | None:
    """Return the model's reply text, or None when it is unreachable, slow, or empty."""
    base_url = str(settings.get("translation_base_url") or DEFAULT_BASE_URL)
    body: dict = {
        "model": model or DEFAULT_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0, "num_predict": num_predict},
    }
    if json_mode:
        body["format"] = "json"
    request = Request(  # noqa: S310
        f"{base_url.rstrip('/')}/api/generate",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            answer = str(json.loads(response.read().decode()).get("response", "")).strip()
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return None
    return answer or None
