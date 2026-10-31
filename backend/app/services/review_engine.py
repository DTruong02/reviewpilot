import re
import time
from collections.abc import Sequence
from difflib import SequenceMatcher

from pydantic import ValidationError

from app.core.config import settings
from app.schemas.review import (
    LLMFindings,
    ReviewFinding,
    ReviewResult,
)
from app.services.context_builder import ContextBuilder
from app.services.github import GitHubClient
from app.services.llm import LLMClient


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


class ReviewEngine:
    def __init__(
        self,
        github: GitHubClient,
        llm: LLMClient,
        *,
        context_builder: ContextBuilder | None = None,
        max_findings: int | None = None,
        validation_attempts: int = 2,
    ) -> None:
        self.github = github
        self.llm = llm
        self.context_builder = context_builder or ContextBuilder()
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
    ) -> ReviewResult:
        started = time.perf_counter()
        try:
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

        if not context.files:
            return ReviewResult(
                findings=(), duration_ms=max(0, int((time.perf_counter() - started) * 1000))
            )

        provider_output = None
        parsed: LLMFindings | None = None
        for _ in range(self.validation_attempts):
            try:
                provider_output = await self.llm.review(context)
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
        findings: list[ReviewFinding] = []
        for candidate in parsed.findings:
            if candidate.line_number is None:
                continue
            if candidate.line_number not in changed_lines.get(candidate.file_path, frozenset()):
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
        )
