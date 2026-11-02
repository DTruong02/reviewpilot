from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./reviewpilot.db"
    redis_url: str = "redis://localhost:6379/0"
    github_webhook_secret: str = ""
    github_app_id: int | None = None
    github_app_private_key_path: Path | None = None
    github_api_url: str = "https://api.github.com"
    openai_api_key: str = ""
    openai_model: str = ""
    review_dry_run: bool = True
    review_allow_source_upload: bool = False
    review_context_repository_allowlist: str = ""
    review_max_files: int = Field(default=25, ge=1, le=25)
    review_max_changed_lines: int = Field(default=1500, ge=1, le=100_000)
    review_max_findings: int = Field(default=15, ge=1, le=15)
    review_max_context_chars: int = Field(default=30000, ge=1, le=100_000)
    review_max_context_tokens: int = Field(default=10_000, ge=100, le=20_000)
    review_context_lines: int = Field(default=10, ge=10, le=50)
    review_max_output_tokens: int = Field(default=3000, ge=1, le=8192)
    review_timeout_seconds: float = Field(default=45, ge=1, le=180)
    github_token_cache_skew_seconds: int = Field(default=300, ge=30, le=900)
    review_enqueue_retry_after_seconds: int = Field(default=30, ge=1, le=3600)
    review_reenqueue_after_seconds: int = Field(default=300, ge=30, le=86400)
    review_running_stale_seconds: int = Field(default=1800, ge=60, le=86400)
    review_task_max_retries: int = Field(default=3, ge=0, le=10)
    index_enabled: bool = True
    index_max_files: int = Field(default=5000, ge=1, le=100_000)
    index_max_file_bytes: int = Field(default=1_000_000, ge=1, le=1_000_000)
    index_max_total_bytes: int = Field(default=50_000_000, ge=1_000_000, le=1_000_000_000)
    index_max_tree_entries: int = Field(default=100_000, ge=100, le=200_000)
    index_embedding_model: str = "text-embedding-3-small"
    index_embedding_dimension: int = Field(default=1536, ge=1, le=3072)
    index_embedding_mode: Literal["disabled", "mock", "openai"] = "mock"
    index_embedding_repository_allowlist: str = ""
    index_embedding_timeout_seconds: float = Field(default=45, ge=1, le=180)
    index_stale_seconds: int = Field(default=1800, ge=60, le=86_400)
    index_secret_patterns: str = ""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_ignore_empty=True,
        extra="ignore",
    )


settings = Settings()
