from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.review import ContextProvenance, EvidenceReference

FeedbackValue = Literal["helpful", "false_positive"]
ReviewStatusValue = Literal["queued", "running", "completed", "failed", "superseded"]
SeverityValue = Literal["low", "medium", "high"]
CategoryValue = Literal["bug", "security", "reliability", "maintainability"]


class Page(BaseModel):
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    total: int = Field(ge=0)


class RepositoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner: str
    name: str
    full_name: str
    private: bool
    created_at: datetime
    updated_at: datetime


class RepositoryPage(Page):
    items: list[RepositoryRead]


class PullRequestRead(BaseModel):
    id: UUID
    repository_id: UUID
    repository_full_name: str
    number: int
    title: str
    state: str
    head_sha: str
    html_url: str
    updated_at: datetime
    latest_review_status: ReviewStatusValue | None = None


class PullRequestPage(Page):
    items: list[PullRequestRead]


class PullRequestReviewsPage(Page):
    items: list["ReviewSummary"]


class ReviewSummary(BaseModel):
    id: UUID
    commit_sha: str
    status: ReviewStatusValue
    model_name: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None = None
    error_message: str | None = None


class FindingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    file_path: str
    line_number: int | None
    severity: SeverityValue
    category: CategoryValue
    title: str
    explanation: str
    suggested_fix: str | None
    feedback: FeedbackValue | None
    evidence_refs: list[EvidenceReference] | None = None
    created_at: datetime


class ReviewRead(ReviewSummary):
    pull_request_id: UUID
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None
    findings: list[FindingRead]
    context_mode: Literal["diff_only", "diff_only_fallback", "context_aware"] = "diff_only"
    context_fallback_reason: str | None = None
    context_metadata: list[ContextProvenance] | None = None


class FeedbackUpdate(BaseModel):
    feedback: FeedbackValue


class FeedbackRead(BaseModel):
    id: UUID
    feedback: FeedbackValue


class IndexRequest(BaseModel):
    commit_sha: str = Field(pattern=r"^[0-9a-fA-F]{40}$")


class IndexStatusRead(BaseModel):
    id: UUID
    repository_id: UUID
    commit_sha: str
    status: str
    parser_version: str
    embedding_model: str
    embedding_version: str
    embedding_dimension: int
    embedding_enabled: bool
    files_discovered: int
    files_indexed: int
    files_skipped: int
    symbols_indexed: int
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None
