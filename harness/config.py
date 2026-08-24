"""Environment-driven configuration.

All knobs come from env vars so the same code runs against any
OpenAI-compatible provider (Groq, Google AI Studio, OpenRouter, Ollama, ...)
without code changes. Sensible defaults keep the zero-key demo path working.
"""
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    toolserver_url: str = field(
        default_factory=lambda: os.environ.get("AGENTDESK_TOOLSERVER_URL", "http://localhost:8077")
    )
    llm_base_url: str = field(
        default_factory=lambda: os.environ.get("AGENTDESK_LLM_BASE_URL", "https://api.groq.com/openai/v1")
    )
    llm_model: str = field(
        default_factory=lambda: os.environ.get("AGENTDESK_LLM_MODEL", "llama-3.3-70b-versatile")
    )
    llm_api_key: str = field(default_factory=lambda: os.environ.get("AGENTDESK_LLM_API_KEY", ""))
    max_iterations: int = field(default_factory=lambda: int(os.environ.get("AGENTDESK_MAX_ITERS", "14")))
    request_timeout_s: int = field(default_factory=lambda: int(os.environ.get("AGENTDESK_TIMEOUT_S", "60")))


settings = Settings()
