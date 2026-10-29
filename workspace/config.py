from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE pairs from a local .env without overriding real env vars."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value:
            os.environ.setdefault(key, value)


_load_dotenv(PROJECT_ROOT / ".env")


DEFAULT_MAX_STEPS = 20


def _first_env(*names: str) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


@dataclass
class LLMConfig:
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.0
    max_tokens: int = 4096
    timeout: int = 180
    max_retries: int = 5

    def __post_init__(self) -> None:
        deepseek = bool(os.environ.get("DEEPSEEK_API_KEY"))
        if not self.model:
            self.model = _first_env("LLM_MODEL") or (
                "deepseek-chat" if deepseek else "gpt-4o"
            )
        if not self.api_key:
            self.api_key = (
                _first_env("OPENAI_API_KEY", "LLM_API_KEY", "DEEPSEEK_API_KEY")
                or ""
            )
        if not self.base_url:
            self.base_url = _first_env("OPENAI_BASE_URL", "LLM_BASE_URL") or (
                "https://api.deepseek.com/v1" if deepseek else "https://api.openai.com/v1"
            )
        self.base_url = self.base_url.rstrip("/")
        if self.base_url.endswith("/chat/completions"):
            self.endpoint = self.base_url
        else:
            self.endpoint = f"{self.base_url}/chat/completions"


@dataclass
class AgentConfig:
    max_steps: int = DEFAULT_MAX_STEPS
    max_schema_chars: int = 20000
    max_tool_chars: int = 8000
    max_doc_chars: int = 30000
    query_timeout: int = 120
    exploration_row_limit: int = 60
    retry_invalid_submit: int = 2
    verbose: bool = False
    extra: dict = field(default_factory=dict)


