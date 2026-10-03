"""Application configuration, loaded from environment variables / .env.

Nothing machine-specific is hard-coded; every path comes from configuration.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Repository / storage
    source_root: Path | None = None
    data_root: Path = PROJECT_ROOT / "data"
    chroma_path: Path | None = None

    # Ollama (used from later phases; model names are never hard-coded)
    ollama_base_url: str = "http://localhost:11434"
    ollama_chat_model: str = "qwen2.5-coder:7b"
    ollama_embed_model: str = "nomic-embed-text"

    # Pipeline limits
    log_level: str = "INFO"
    max_context_tokens: int = 8000
    max_source_files: int = 20000
    max_call_depth: int = 2
    enable_git_analysis: bool = False
    enable_test_analysis: bool = True

    # Scanner
    # NoDecode: accept comma-separated env values instead of JSON arrays
    ignore_dirs: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "target",
            "build",
            ".git",
            "node_modules",
            "generated",
            "out",
            ".idea",
            ".gradle",
        ]
    )
    max_file_bytes: int = 2_000_000

    # Indexing: files per analyzer (JVM) call; progress and persistence are per batch
    index_batch_size: int = 50

    # Java analyzer sidecar (JavaParser, requires JDK 17+)
    java_bin: str = "java"
    analyzer_jar: Path = PROJECT_ROOT / "java-analyzer" / "target" / "java-analyzer.jar"
    analyzer_timeout_seconds: int = 300

    @field_validator("ignore_dirs", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("source_root", "chroma_path", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        return None if value == "" else value

    @property
    def effective_chroma_path(self) -> Path:
        return self.chroma_path or self.data_root / "indexes" / "chroma"

    @property
    def db_path(self) -> Path:
        return self.data_root / "knowledge" / "code_helper.sqlite3"


@lru_cache
def get_settings() -> Settings:
    return Settings()
