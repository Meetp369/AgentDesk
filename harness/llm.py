"""Thin OpenAI-compatible chat-completions client.

Deliberately raw HTTP instead of a provider SDK:
  - one code path works for Groq, Google AI Studio, OpenRouter, Ollama, etc.
  - the request/response wire format stays visible (useful for debugging and
    for explaining exactly what the harness sends).

Handles the failure modes free tiers actually exhibit: 429 rate limits
(honors Retry-After), transient 5xx, and malformed tool-call arguments.
"""
from __future__ import annotations

import json
import time

import requests

from .config import settings


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, base_url: str | None = None, model: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.model = model or settings.llm_model
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        if not self.api_key:
            raise LLMError(
                "No API key. Set AGENTDESK_LLM_API_KEY (and optionally AGENTDESK_LLM_BASE_URL / "
                "AGENTDESK_LLM_MODEL), or run with --mode baseline which needs no key."
            )

    def chat(self, messages: list[dict], tools: list[dict], temperature: float = 0.1) -> dict:
        """One chat-completion turn. Returns the assistant message dict."""
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": temperature,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last = None
        for attempt in range(4):
            try:
                r = requests.post(f"{self.base_url}/chat/completions", json=payload,
                                  headers=headers, timeout=settings.request_timeout_s)
            except (requests.ConnectionError, requests.Timeout) as e:
                last = str(e)
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 429:
                wait = float(r.headers.get("retry-after", 2 * (attempt + 1)))
                time.sleep(min(wait, 20))
                last = "rate limited (429)"
                continue
            if r.status_code >= 500:
                last = f"provider {r.status_code}"
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise LLMError(f"LLM request failed {r.status_code}: {r.text[:400]}")
            data = r.json()
            try:
                return data["choices"][0]["message"]
            except (KeyError, IndexError) as e:
                raise LLMError(f"unexpected response shape: {e}: {json.dumps(data)[:400]}")
        raise LLMError(f"LLM unreachable after retries: {last}")


def parse_tool_calls(message: dict) -> list[tuple[str, str, dict]]:
    """Extract (call_id, tool_name, args) triples; tolerates malformed JSON args."""
    out = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function", {})
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw)
            if not isinstance(args, dict):
                args = {"_malformed": raw}
        except json.JSONDecodeError:
            args = {"_malformed": raw}
        out.append((tc.get("id", ""), fn.get("name", ""), args))
    return out
