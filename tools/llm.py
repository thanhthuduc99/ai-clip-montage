"""Gọi OpenRouter chat completions — dùng chung cho analyzer/script/matcher.
Log usage token mỗi call để biết chi phí."""
from __future__ import annotations

import json
import sys

import requests

import config


class LLMError(RuntimeError):
    pass


def chat_json(messages: list[dict], model: str = "", temperature: float = 0.7,
              timeout: int = 90) -> dict:
    """Gọi OpenRouter với response_format json_object; trả dict đã parse."""
    key = config.openrouter_key()
    if not key:
        raise LLMError("Thiếu OPENROUTER_KEY trong .env.")
    model = model or config.TEXT_MODEL
    r = requests.post(
        config.OPENROUTER_URL,
        timeout=timeout,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": temperature,
        },
    )
    if r.status_code != 200:
        raise LLMError(f"OpenRouter {r.status_code}: {r.text[:300]}")
    data = r.json()
    usage = data.get("usage") or {}
    print(f"[llm] {model} tokens in={usage.get('prompt_tokens')} "
          f"out={usage.get('completion_tokens')}", file=sys.stderr)
    content = data["choices"][0]["message"]["content"]
    try:
        return json.loads(content)
    except (ValueError, TypeError) as exc:
        raise LLMError(f"LLM trả JSON hỏng: {content[:200]}") from exc


def image_content(b64_jpeg: str) -> dict:
    """1 phần tử image cho message content nhiều ảnh."""
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{b64_jpeg}"},
    }
