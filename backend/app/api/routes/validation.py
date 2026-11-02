"""Authenticated validation planning and no-execution simulation API (Milestone D)."""

import hashlib
import hmac
import json
import re
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_session
from app.models.entities import (
    Finding,
    PullRequest,
    Repository,
    Review,
    ValidationApproval,
    ValidationPlan,
    ValidationRun,
)

router = APIRouter(prefix="/api", tags=["validation planning"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
POLICY_VERSION = "validation-plan-v1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class PlanData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    runner_template: Literal["pytest_targeted", "ruff_targeted", "npm_test_targeted"]
    expected_behavior: str = Field(min_length=1, max_length=500)
    test_input: str = Field(min_length=1, max_length=500)
    expected_observation: str = Field(min_length=1, max_length=500)


class PlanRead(BaseModel):
    id: UUID
    finding_id: UUID
    repository: str
    commit_sha: str
    policy_version: str
    plan: PlanData
    plan_hash: str
    status: str
    approved: bool


class RunRead(BaseModel):
    id: UUID
    plan_id: UUID
    status: str
    conclusion: str
    executor: str
    observation: str


async def operator_auth(authorization: Annotated[str | None, Header()] = None) -> str:
    configured = settings.validation_api_token
    try:
        configured_bytes = configured.encode("utf-8")
    except UnicodeEncodeError:
        configured_bytes = b""
    if len(configured_bytes) < 32:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "validation_not_configured",
                "message": "Validation planning is not configured.",
            },
        )
    supplied = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    if not supplied or not hmac.compare_digest(supplied.encode(), configured_bytes):
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "A valid operator token is required."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return "single_operator"


def _allowed_repositories() -> set[str]:
    values = {
        entry.strip().casefold() for entry in settings.validation_allowed_repositories.split(",")
    }
    return {value for value in values if REPO_RE.fullmatch(value)}


def _require_repo(full_name: str) -> None:
    if full_name.casefold() not in _allowed_repositories():
        raise HTTPException(
            status_code=403,
            detail={
                "code": "repository_not_allowed",
                "message": "This repository is not allowlisted for validation planning.",
            },
        )


def _plan_hash(plan: dict, repo: str, sha: str, policy: str, finding_id: UUID) -> str:
    payload = {
        "plan": plan,
        "repository": repo.casefold(),
        "commit_sha": sha,
        "policy_version": policy,
        "finding_id": str(finding_id),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _plan_context(session: AsyncSession, plan_id: UUID):
    row = await session.execute(
        select(ValidationPlan, Repository.full_name)
        .join(Repository, Repository.id == ValidationPlan.repository_id)
        .where(ValidationPlan.id == plan_id)
    )
    result = row.first()
    if result is None:
        raise HTTPException(
            404, detail={"code": "plan_not_found", "message": "Validation plan was not found."}
        )
    plan, full_name = result
    _require_repo(full_name)
    actual = _plan_hash(
        plan.plan_json, full_name, plan.commit_sha, plan.policy_version, plan.finding_id
    )
    if not hmac.compare_digest(actual, plan.plan_hash):
        plan.status = "invalidated"
        approval = await session.scalar(
            select(ValidationApproval).where(ValidationApproval.plan_id == plan.id)
        )
        if approval is not None:
            await session.delete(approval)
        await session.commit()
        raise HTTPException(
            409,
            detail={
                "code": "plan_integrity_failed",
                "message": "Plan changed after creation; approval is invalid.",
            },
        )
    return plan, full_name


async def _plan_response(session: AsyncSession, plan: ValidationPlan, full_name: str) -> PlanRead:
    approval = await session.scalar(
        select(ValidationApproval).where(ValidationApproval.plan_id == plan.id)
    )
    valid_approval = bool(
        approval
        and hmac.compare_digest(approval.plan_hash, plan.plan_hash)
        and approval.repository_id == plan.repository_id
        and approval.commit_sha == plan.commit_sha
    )
    return PlanRead(
        id=plan.id,
        finding_id=plan.finding_id,
        repository=full_name,
        commit_sha=plan.commit_sha,
        policy_version=plan.policy_version,
        plan=PlanData.model_validate(plan.plan_json),
        plan_hash=plan.plan_hash,
        status=plan.status,
        approved=valid_approval,
    )


@router.post(
    "/findings/{finding_id}/validation-plans",
    response_model=PlanRead,
    dependencies=[Depends(operator_auth)],
)
async def create_plan(finding_id: UUID, session: SessionDep) -> PlanRead:
    row = await session.execute(
        select(Finding, Review, PullRequest, Repository)
        .join(Review, Finding.review_id == Review.id)
        .join(PullRequest, Review.pull_request_id == PullRequest.id)
        .join(Repository, PullRequest.repository_id == Repository.id)
        .where(Finding.id == finding_id)
    )
    context = row.first()
    if context is None:
        raise HTTPException(
            404, detail={"code": "finding_not_found", "message": "Finding was not found."}
        )
    finding, review, _pr, repo = context
    _require_repo(repo.full_name)
    if not SHA_RE.fullmatch(review.commit_sha):
        raise HTTPException(
            409,
            detail={
                "code": "invalid_review_sha",
                "message": "Review has no valid immutable commit SHA.",
            },
        )
    plan = PlanData(
        runner_template="pytest_targeted",
        expected_behavior=(
            f"A focused check should cover the reported {finding.category.value} concern."
        ),
        test_input=(
            f"Finding {finding.id} in {finding.file_path[:180]} at line "
            f"{finding.line_number or 'unspecified'}; use a bounded fixture only."
        ),
        expected_observation=(
            "A future isolated validator would report pass or fail; "
            "this plan itself observes nothing."
        ),
    ).model_dump(mode="json")
    record = ValidationPlan(
        finding_id=finding.id,
        repository_id=repo.id,
        commit_sha=review.commit_sha,
        policy_version=POLICY_VERSION,
        plan_json=plan,
        plan_hash=_plan_hash(plan, repo.full_name, review.commit_sha, POLICY_VERSION, finding.id),
        status="proposed",
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return await _plan_response(session, record, repo.full_name)


@router.get(
    "/validation-plans/{plan_id}", response_model=PlanRead, dependencies=[Depends(operator_auth)]
)
async def get_plan(plan_id: UUID, session: SessionDep) -> PlanRead:
    plan, full_name = await _plan_context(session, plan_id)
    return await _plan_response(session, plan, full_name)


@router.post(
    "/validation-plans/{plan_id}/approve",
    response_model=PlanRead,
    dependencies=[Depends(operator_auth)],
)
async def approve_plan(plan_id: UUID, session: SessionDep) -> PlanRead:
    plan, full_name = await _plan_context(session, plan_id)
    approval = await session.scalar(
        select(ValidationApproval).where(ValidationApproval.plan_id == plan.id)
    )
    if approval is None:
        approval = ValidationApproval(
            plan_id=plan.id,
            plan_hash=plan.plan_hash,
            repository_id=plan.repository_id,
            commit_sha=plan.commit_sha,
            approver="single_operator",
        )
        session.add(approval)
    else:
        approval.plan_hash = plan.plan_hash
        approval.repository_id = plan.repository_id
        approval.commit_sha = plan.commit_sha
    plan.status = "approved"
    await session.commit()
    return await _plan_response(session, plan, full_name)


@router.post(
    "/validation-plans/{plan_id}/run", response_model=RunRead, dependencies=[Depends(operator_auth)]
)
async def simulate_run(plan_id: UUID, session: SessionDep) -> RunRead:
    plan, _full_name = await _plan_context(session, plan_id)
    approval = await session.scalar(
        select(ValidationApproval).where(ValidationApproval.plan_id == plan.id)
    )
    if not approval or not (
        hmac.compare_digest(approval.plan_hash, plan.plan_hash)
        and approval.repository_id == plan.repository_id
        and approval.commit_sha == plan.commit_sha
    ):
        raise HTTPException(
            409,
            detail={
                "code": "approval_required",
                "message": "Approve this exact plan before requesting its simulation.",
            },
        )
    run = ValidationRun(
        plan_id=plan.id,
        status="not_attempted",
        conclusion="inconclusive",
        executor="mock_no_execution",
        observation="No code was run. No command was queued or started.",
    )
    session.add(run)
    plan.status = "simulation_not_attempted"
    await session.commit()
    await session.refresh(run)
    return RunRead.model_validate(run, from_attributes=True)


@router.get(
    "/validation-runs/{run_id}", response_model=RunRead, dependencies=[Depends(operator_auth)]
)
async def get_run(run_id: UUID, session: SessionDep) -> RunRead:
    row = await session.execute(
        select(ValidationRun, ValidationPlan.id, Repository.full_name)
        .join(ValidationPlan, ValidationPlan.id == ValidationRun.plan_id)
        .join(Repository, Repository.id == ValidationPlan.repository_id)
        .where(ValidationRun.id == run_id)
    )
    result = row.first()
    if result is None:
        raise HTTPException(
            404, detail={"code": "run_not_found", "message": "Validation run was not found."}
        )
    run, _plan_id, full_name = result
    _require_repo(full_name)
    return RunRead.model_validate(run, from_attributes=True)
