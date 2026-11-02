import re
import time
from collections.abc import Sequence
from difflib import SequenceMatcher
from uuid import UUID

from pydantic import ValidationError

from app.core.config import settings
from app.schemas.review import (
    ContextProvenance,
    LLMFindings,
    ReviewFinding,
    ReviewResult,
)
from app.services.context_builder import ContextBuilder
from app.services.context_retriever import ContextRetrievalResult, RepositoryContextRetriever
from app.services.github import GitHubClient
from app.services.llm import LLMClient, LLMClientError


class ReviewEngineError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _normalized(text: str) -> str:
    return re.sub(r"\W+", " ", text.casefold()).strip()


def _is_duplicate(candidate: ReviewFinding, existing: Sequence[ReviewFinding]) -> bool:
    title = _normalized(candidate.title)
    explanation = _normalized(candidate.explanation)
    for finding in existing:
        if candidate.file_path != finding.file_path:
            continue
        if candidate.line_number == finding.line_number and title == _normalized(finding.title):
            return True
        ratio = SequenceMatcher(None, title, _normalized(finding.title)).ratio()
        explanation_ratio = SequenceMatcher(
            None, explanation, _normalized(finding.explanation)
        ).ratio()
        if ratio >= 0.92 and explanation_ratio >= 0.80:
            return True
    return False


def _context_upload_allowed(repository: str) -> bool:
    if settings.review_dry_run:
        return True
    if not settings.review_allow_source_upload:
        return False
    allowed = {
        item.strip().casefold()
        for item in settings.review_context_repository_allowlist.split(",")
        if item.strip()
    }
    return repository.casefold() in allowed


class ReviewEngine:
    def __init__(
        self,
        github: GitHubClient,
        llm: LLMClient,
        *,
        context_builder: ContextBuilder | None = None,
        context_retriever: RepositoryContextRetriever | None = None,
        max_findings: int | None = None,
        validation_attempts: int = 2,
    ) -> None:
        self.github = github
        self.llm = llm
        self.context_builder = context_builder or ContextBuilder()
        self.context_retriever = context_retriever
        self.max_findings = max(1, min(max_findings or settings.review_max_findings, 15))
        self.validation_attempts = max(1, min(validation_attempts, 2))

    async def review_pull_request(
        self,
        *,
        installation_id: int,
        owner: str,
        repository: str,
        pull_request_number: int,
        pull_request_title: str,
        head_sha: str,
        repository_id: UUID | None = None,
    ) -> ReviewResult:
        started = time.perf_counter()
        try:
            current_head_sha = await self.github.fetch_pull_request_head_sha(
                installation_id, owner, repository, pull_request_number
            )
            if current_head_sha != head_sha.lower():
                return ReviewResult(
                    findings=(),
                    duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
                    context_mode="diff_only_fallback",
                    context_fallback_reason="pull_request_head_changed",
                    stale_commit=True,
                )
            changed_files = await self.github.fetch_pull_request_files(
                installation_id, owner, repository, pull_request_number
            )
            context = await self.context_builder.build(
                repository=f"{owner}/{repository}",
                pull_request_number=pull_request_number,
                pull_request_title=pull_request_title,
                head_sha=head_sha,
                changed_files=changed_files,
                fetch_content=lambda path: self.github.fetch_file_content(
                    installation_id, owner, repository, path, head_sha
                ),
            )
        except Exception as exc:
            code = getattr(exc, "code", "github_review_failed")
            raise ReviewEngineError(str(code)) from None

        retrieval = ContextRetrievalResult("diff_only_fallback", "retriever_unavailable", ())
        if self.context_retriever is not None and repository_id is not None:
            if not _context_upload_allowed(f"{owner}/{repository}"):
                retrieval = ContextRetrievalResult(
                    "diff_only_fallback", "context_source_upload_not_allowed", ()
                )
            else:
                used_tokens = (len(context.model_dump_json()) + 3) // 4
                remaining_tokens = max(0, settings.review_max_context_tokens - used_tokens)
                try:
                    retrieval = await self.context_retriever.retrieve(
                        repository_id=repository_id,
                        head_sha=head_sha,
                        changed_files=changed_files,
                        fetch_content=lambda path, sha: self.github.fetch_file_content(
                            installation_id, owner, repository, path, sha
                        ),
                        token_budget=remaining_tokens,
                    )
                except Exception:
                    # Retrieval failure must not block the existing diff-only review.
                    retrieval = ContextRetrievalResult(
                        "diff_only_fallback", "retrieval_unavailable", ()
                    )
        elif repository_id is None:
            retrieval = ContextRetrievalResult(
                "diff_only_fallback", "repository_id_unavailable", ()
            )
        context = context.model_copy(
            update={
                "context_mode": retrieval.mode,
                "context_fallback_reason": retrieval.reason,
                "retrieved_context": retrieval.items,
            }
        )

        try:
            current_head_sha = await self.github.fetch_pull_request_head_sha(
                installation_id, owner, repository, pull_request_number
            )
        except Exception as exc:
            code = getattr(exc, "code", "github_review_failed")
            raise ReviewEngineError(str(code)) from None
        if current_head_sha != head_sha.lower():
            return ReviewResult(
                findings=(),
                duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
                context_mode="diff_only_fallback",
                context_fallback_reason="pull_request_head_changed",
                stale_commit=True,
            )

        if not context.files:
            return ReviewResult(
                findings=(), duration_ms=max(0, int((time.perf_counter() - started) * 1000))
            )

        provider_output = None
        parsed: LLMFindings | None = None
        for _ in range(self.validation_attempts):
            try:
                provider_output = await self.llm.review(context)
            except LLMClientError as exc:
                if exc.code in {
                    "openai_empty_structured_output",
                    "openai_invalid_structured_output",
                }:
                    provider_output = None
                    parsed = None
                    continue
                raise ReviewEngineError(exc.code) from None
            except Exception as exc:
                code = getattr(exc, "code", "llm_review_failed")
                raise ReviewEngineError(str(code)) from None
            try:
                parsed = LLMFindings.model_validate(provider_output.output)
                break
            except (ValidationError, TypeError, ValueError):
                parsed = None
        if parsed is None or provider_output is None:
            raise ReviewEngineError("llm_invalid_output")

        changed_lines = {file.file_path: file.added_lines for file in context.files}
        valid_evidence = {item.symbol_id: item for item in context.retrieved_context}
        findings: list[ReviewFinding] = []
        for candidate in parsed.findings:
            if candidate.line_number is None:
                continue
            if candidate.line_number not in changed_lines.get(candidate.file_path, frozenset()):
                continue
            if any(
                evidence.symbol_id not in valid_evidence
                or (
                    valid_evidence[evidence.symbol_id].file_path != evidence.file_path
                    or valid_evidence[evidence.symbol_id].start_line != evidence.start_line
                    or valid_evidence[evidence.symbol_id].end_line != evidence.end_line
                )
                for evidence in candidate.evidence_refs
            ):
                continue
            finding = ReviewFinding.model_validate(candidate.model_dump())
            if _is_duplicate(finding, findings):
                continue
            findings.append(finding)
            if len(findings) >= self.max_findings:
                break

        return ReviewResult(
            findings=tuple(findings),
            model_name=provider_output.model_name,
            input_tokens=provider_output.input_tokens,
            output_tokens=provider_output.output_tokens,
            duration_ms=max(
                provider_output.duration_ms,
                int((time.perf_counter() - started) * 1000),
            ),
            context_mode=context.context_mode,
            context_fallback_reason=context.context_fallback_reason,
            context_metadata=tuple(
                ContextProvenance(
                    file_path=item.file_path,
                    symbol_id=item.symbol_id,
                    start_line=item.start_line,
                    end_line=item.end_line,
                    commit_sha=item.commit_sha,
                    retrieval_reason=item.retrieval_reason,
                    score=item.score,
                )
                for item in context.retrieved_context
            ),
        )
