from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PullRequestFile(BaseModel):
    """Minimal file metadata returned by GitHub's pull request files endpoint."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    path: str = Field(min_length=1, max_length=1024, validation_alias="filename")
    status: str = Field(min_length=1, max_length=30)
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    patch: str | None = None


class ReviewFile(BaseModel):
    """Bounded input for a single changed file; line numbers are new-side lines."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file_path: str = Field(min_length=1, max_length=1024)
    patch: str = Field(max_length=30000)
    context: str = Field(max_length=30000)
    added_lines: frozenset[int]
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)


class RetrievedCodeContext(BaseModel):
    """A bounded, source-backed snippet included in the model request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file_path: str = Field(min_length=1, max_length=1024)
    symbol_id: str = Field(min_length=1, max_length=64)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    retrieval_reason: Literal[
        "changed_symbol", "imported_file", "importer_file", "semantic_similarity"
    ]
    score: float = Field(ge=0, le=1)
    snippet: str = Field(min_length=1, max_length=8000)


class ReviewContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str = Field(min_length=3, max_length=512)
    pull_request_number: int = Field(gt=0)
    pull_request_title: str = Field(max_length=1000)
    head_sha: str = Field(pattern=r"^[0-9a-fA-F]{40}$")
    files: tuple[ReviewFile, ...] = Field(max_length=25)
    context_mode: Literal["diff_only", "diff_only_fallback", "context_aware"] = "diff_only"
    context_fallback_reason: str | None = Field(default=None, max_length=100)
    retrieved_context: tuple[RetrievedCodeContext, ...] = Field(default=(), max_length=40)


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol_id: str = Field(min_length=1, max_length=64)
    file_path: str = Field(min_length=1, max_length=1024)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class ContextProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    file_path: str = Field(min_length=1, max_length=1024)
    symbol_id: str = Field(min_length=1, max_length=64)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    retrieval_reason: Literal[
        "changed_symbol", "imported_file", "importer_file", "semantic_similarity"
    ]
    score: float = Field(ge=0, le=1)


class FindingCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(min_length=1, max_length=1024)
    line_number: int | None = Field(default=None, ge=1)
    severity: Literal["low", "medium", "high"]
    category: Literal["bug", "security", "reliability", "maintainability"]
    title: str = Field(min_length=1, max_length=120)
    explanation: str = Field(min_length=1, max_length=1200)
    suggested_fix: str | None = Field(default=None, max_length=1200)
    evidence_refs: list[EvidenceReference] = Field(default_factory=list, max_length=5)


class LLMFindings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[FindingCandidate] = Field(max_length=100)


class ReviewFinding(FindingCandidate):
    model_config = ConfigDict(extra="forbid", frozen=True)
    line_number: int = Field(ge=1)


class ProviderOutput(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    output: object
    model_name: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    duration_ms: int = Field(ge=0)


class ReviewResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    findings: tuple[ReviewFinding, ...]
    model_name: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: int
    context_mode: Literal["diff_only", "diff_only_fallback", "context_aware"] = "diff_only"
    context_fallback_reason: str | None = None
    context_metadata: tuple[ContextProvenance, ...] = ()
    stale_commit: bool = False
