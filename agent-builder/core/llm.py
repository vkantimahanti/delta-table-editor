"""OpenAI-compatible chat completions via LiteLLM gateway."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import litellm_settings

logger = logging.getLogger("agent_builder.llm")


def chat_completion(
    *,
    system: str,
    user: str,
    temperature: float = 0.2,
    timeout: float = 120.0,
) -> str:
    cfg = litellm_settings()
    url = f"{cfg['base_url']}/chat/completions"
    payload: dict[str, Any] = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
    }
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    logger.info("Calling LiteLLM model=%s", cfg["model"])
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"LiteLLM error {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("LiteLLM returned no choices")
    content = choices[0].get("message", {}).get("content", "")
    if not str(content).strip():
        raise RuntimeError("LiteLLM returned empty content")
    return str(content).strip()
