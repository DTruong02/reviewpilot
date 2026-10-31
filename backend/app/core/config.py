from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./reviewpilot.db"
    github_webhook_secret: str = ""
    github_app_id: int | None = None
    github_app_private_key_path: Path | None = None
    github_api_url: str = "https://api.github.com"
    openai_api_key: str = ""
    openai_model: str = ""
    review_dry_run: bool = True
    review_allow_source_upload: bool = False
    review_max_files: int = Field(default=25, ge=1, le=25)
    review_max_changed_lines: int = Field(default=1500, ge=1, le=100_000)
    review_max_findings: int = Field(default=15, ge=1, le=15)
    review_max_context_chars: int = Field(default=30000, ge=1, le=100_000)
    review_context_lines: int = Field(default=10, ge=10, le=50)
    review_max_output_tokens: int = Field(default=3000, ge=1, le=8192)
    review_timeout_seconds: float = Field(default=45, ge=1, le=180)
    github_token_cache_skew_seconds: int = Field(default=300, ge=30, le=900)

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")


settings = Settings()
