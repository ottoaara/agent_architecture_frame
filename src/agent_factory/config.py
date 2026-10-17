"""Environment-driven settings. Secrets stay in the environment / .env (never in source)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    provider: str = "mock"
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    ollama_model: str = "qwen3.8"
    ollama_base_url: str = "http://127.0.0.1:11434"
    data_dir: Path = Path("var")
    api_url: str = "http://127.0.0.1:8000"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "factory.db"

    @property
    def checkpoint_path(self) -> Path:
        return self.data_dir / "checkpoints.db"

    def workspace_for(self, run_id: str) -> Path:
        return self.data_dir / "runs" / run_id

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        env = os.environ.get
        return cls(
            provider=env("AGENT_FACTORY_PROVIDER", cls.provider),
            model=env("AGENT_FACTORY_MODEL", cls.model),
            effort=env("AGENT_FACTORY_EFFORT", cls.effort),
            ollama_model=env("OLLAMA_MODEL", cls.ollama_model),
            ollama_base_url=env("OLLAMA_BASE_URL", cls.ollama_base_url),
            data_dir=Path(env("AGENT_FACTORY_DATA_DIR", str(cls.data_dir))),
            api_url=env("AGENT_FACTORY_API_URL", cls.api_url),
        )
