from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.models.entities import Finding, PullRequest, Repository, Review, Severity
from app.schemas.api import (
    FeedbackRead,
    FeedbackUpdate,
    FindingRead,
    PullRequestPage,
    PullRequestRead,
    PullRequestReviewsPage,
    RepositoryPage,
    RepositoryRead,
    ReviewRead,
    ReviewSummary,
)

router = APIRouter(prefix="/api", tags=["review data"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]

PUBLIC_FAILURE = "The review could not be completed."


def _review_summary(review: Review) -> ReviewSummary:
    # Internal exception text is deliberately never returned to API clients.
    public_code = "review_failed" if review.status == "failed" and review.error_code else None
    return ReviewSummary(
        id=review.id,
        commit_sha=review.commit_sha,
        status=review.status.value,
        model_name=review.model_name,
        created_at=review.created_at,
        started_at=review.started_at,
        completed_at=review.completed_at,
        error_code=public_code,
        error_message=PUBLIC_FAILURE if public_code else None,
    )


def _pull_request_read(pr: PullRequest, repository_full_name: str, status) -> PullRequestRead:
    return PullRequestRead(
        id=pr.id,
        repository_id=pr.repository_id,
        repository_full_name=repository_full_name,
        number=pr.github_pr_number,
        title=pr.title,
        state=pr.state,
        head_sha=pr.head_sha,
        html_url=pr.html_url,
        updated_at=pr.updated_at,
        latest_review_status=status.value if status else None,
    )


@router.get("/repositories", response_model=RepositoryPage)
async def list_repositories(
    session: SessionDep, limit: Limit = 50, offset: Offset = 0
) -> RepositoryPage:
    total = await session.scalar(select(func.count()).select_from(Repository)) or 0
    result = await session.scalars(
        select(Repository)
        .order_by(Repository.full_name.asc(), Repository.id.asc())
        .offset(offset)
        .limit(limit)
    )
    return RepositoryPage(
        items=[RepositoryRead.model_validate(repo) for repo in result],
        limit=limit,
        offset=offset,
        total=total,
    )


@router.get("/repositories/{repository_id}/pull-requests", response_model=PullRequestPage)
async def list_pull_requests(
    repository_id: UUID,
    session: SessionDep,
    limit: Limit = 50,
    offset: Offset = 0,
) -> PullRequestPage:
    repository = await session.get(Repository, repository_id)
    if repository is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "repository_not_found", "message": "Repository was not found."},
        )
    total = (
        await session.scalar(
            select(func.count())
            .select_from(PullRequest)
            .where(PullRequest.repository_id == repository_id)
        )
        or 0
    )
    latest_status = (
        select(Review.status)
        .where(Review.pull_request_id == PullRequest.id)
        .order_by(Review.created_at.desc(), Review.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    rows = await session.execute(
        select(PullRequest, latest_status)
        .where(PullRequest.repository_id == repository_id)
        .order_by(PullRequest.updated_at.desc(), PullRequest.id.asc())
        .offset(offset)
        .limit(limit)
    )
    return PullRequestPage(
        items=[_pull_request_read(pr, repository.full_name, status) for pr, status in rows],
        limit=limit,
        offset=offset,
        total=total,
    )


@router.get("/pull-requests/{pull_request_id}", response_model=PullRequestRead)
async def get_pull_request(pull_request_id: UUID, session: SessionDep) -> PullRequestRead:
    latest_status = (
        select(Review.status)
        .where(Review.pull_request_id == PullRequest.id)
        .order_by(Review.created_at.desc(), Review.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    row = (
        await session.execute(
            select(PullRequest, Repository.full_name, latest_status)
            .join(Repository, Repository.id == PullRequest.repository_id)
            .where(PullRequest.id == pull_request_id)
        )
    ).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "pull_request_not_found", "message": "Pull request was not found."},
        )
    pr, full_name, status = row
    return _pull_request_read(pr, full_name, status)


@router.get("/pull-requests/{pull_request_id}/reviews", response_model=PullRequestReviewsPage)
async def list_pull_request_reviews(
    pull_request_id: UUID,
    session: SessionDep,
    limit: Limit = 50,
    offset: Offset = 0,
) -> PullRequestReviewsPage:
    if await session.get(PullRequest, pull_request_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "pull_request_not_found", "message": "Pull request was not found."},
        )
    total = (
        await session.scalar(
            select(func.count())
            .select_from(Review)
            .where(Review.pull_request_id == pull_request_id)
        )
        or 0
    )
    reviews = await session.scalars(
        select(Review)
        .where(Review.pull_request_id == pull_request_id)
        .order_by(Review.created_at.desc(), Review.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return PullRequestReviewsPage(
        items=[_review_summary(review) for review in reviews],
        limit=limit,
        offset=offset,
        total=total,
    )


@router.get("/reviews/{review_id}", response_model=ReviewRead)
async def get_review(review_id: UUID, session: SessionDep) -> ReviewRead:
    review = await session.get(Review, review_id)
    if review is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "review_not_found", "message": "Review was not found."},
        )
    findings = await session.scalars(
        select(Finding)
        .where(Finding.review_id == review_id)
        .order_by(
            case(
                (Finding.severity == Severity.high, 0),
                (Finding.severity == Severity.medium, 1),
                (Finding.severity == Severity.low, 2),
                else_=3,
            ),
            Finding.file_path.asc(),
            Finding.line_number.asc(),
        )
    )
    return ReviewRead(
        **_review_summary(review).model_dump(),
        pull_request_id=review.pull_request_id,
        input_tokens=review.input_tokens,
        output_tokens=review.output_tokens,
        duration_ms=review.duration_ms,
        context_mode=review.context_mode,
        context_fallback_reason=review.context_fallback_reason,
        context_metadata=review.context_metadata,
        findings=[FindingRead.model_validate(finding) for finding in findings],
    )


@router.patch("/findings/{finding_id}/feedback", response_model=FeedbackRead)
async def set_finding_feedback(
    finding_id: UUID, payload: FeedbackUpdate, session: SessionDep
) -> FeedbackRead:
    finding = await session.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "finding_not_found", "message": "Finding was not found."},
        )
    finding.feedback = payload.feedback
    await session.commit()
    return FeedbackRead(id=finding.id, feedback=payload.feedback)
