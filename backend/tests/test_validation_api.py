from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models.entities import (
    Category,
    Finding,
    Installation,
    PullRequest,
    Repository,
    Review,
    ReviewStatus,
    Severity,
    ValidationApproval,
    ValidationPlan,
    ValidationRun,
)


@pytest.fixture
async def validation_api(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    async with sessions.begin() as session:
        installation = Installation(github_installation_id=1234, account_login="octo")
        session.add(installation)
        await session.flush()
        repository = Repository(
            installation_id=installation.id,
            github_repository_id=5678,
            owner="octo",
            name="demo",
            full_name="octo/demo",
            private=True,
        )
        session.add(repository)
        await session.flush()
        pr = PullRequest(
            repository_id=repository.id,
            github_pr_number=7,
            title="Fix result",
            state="open",
            head_sha="a" * 40,
            html_url="https://github.com/octo/demo/pull/7",
        )
        session.add(pr)
        await session.flush()
        review = Review(
            pull_request_id=pr.id,
            commit_sha="b" * 40,
            status=ReviewStatus.completed,
            model_name="fake",
        )
        session.add(review)
        await session.flush()
        finding = Finding(
            review_id=review.id,
            file_path="src/main.py",
            line_number=12,
            severity=Severity.high,
            category=Category.bug,
            title="Wrong result",
            explanation="The changed branch returns early.",
        )
        session.add(finding)
        await session.flush()
        ids = {"repository": repository.id, "finding": finding.id, "review": review.id}
    monkeypatch.setattr(settings, "validation_api_token", "test-operator-secret")
    monkeypatch.setattr(settings, "validation_allowed_repositories", "octo/demo")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, sessions, ids
    app.dependency_overrides.clear()
    await engine.dispose()


def auth():
    return {"Authorization": "Bearer test-operator-secret"}


@pytest.mark.asyncio
async def test_validation_endpoints_fail_closed_without_auth(validation_api, monkeypatch):
    client, _, ids = validation_api
    monkeypatch.setattr(settings, "validation_api_token", "")
    response = await client.post(f"/api/findings/{ids['finding']}/validation-plans")
    assert response.status_code == 503
    monkeypatch.setattr(settings, "validation_api_token", "configured-secret")
    for method, path in [
        ("post", f"/api/findings/{ids['finding']}/validation-plans"),
        ("post", "/api/validation-plans/00000000-0000-0000-0000-000000000000/approve"),
        ("post", "/api/validation-plans/00000000-0000-0000-0000-000000000000/run"),
        ("get", "/api/validation-plans/00000000-0000-0000-0000-000000000000"),
        ("get", "/api/validation-runs/00000000-0000-0000-0000-000000000000"),
    ]:
        result = await client.request(method, path)
        assert result.status_code == 401


@pytest.mark.asyncio
async def test_plan_is_linked_to_finding_repository_and_review_sha(validation_api):
    client, _, ids = validation_api
    created = await client.post(f"/api/findings/{ids['finding']}/validation-plans", headers=auth())
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["finding_id"] == str(ids["finding"])
    assert body["repository"] == "octo/demo"
    assert body["commit_sha"] == "b" * 40  # finding belongs to the historical immutable review
    assert body["plan"]["runner_template"] == "pytest_targeted"
    assert len(body["plan_hash"]) == 64
    assert body["status"] == "proposed"
    assert body["approved"] is False


@pytest.mark.asyncio
async def test_simulation_requires_approval_and_never_runs_or_queues_code(validation_api):
    client, sessions, ids = validation_api
    created = await client.post(f"/api/findings/{ids['finding']}/validation-plans", headers=auth())
    plan_id = UUID(created.json()["id"])
    denied = await client.post(f"/api/validation-plans/{plan_id}/run", headers=auth())
    assert denied.status_code == 409
    approved = await client.post(f"/api/validation-plans/{plan_id}/approve", headers=auth())
    assert approved.status_code == 200
    assert approved.json()["approved"] is True
    simulated = await client.post(f"/api/validation-plans/{plan_id}/run", headers=auth())
    assert simulated.status_code == 200
    assert simulated.json()["status"] == "not_attempted"
    assert simulated.json()["conclusion"] == "inconclusive"
    assert simulated.json()["executor"] == "mock_no_execution"
    assert "No code was run" in simulated.json()["observation"]
    detail = await client.get(f"/api/validation-runs/{simulated.json()['id']}", headers=auth())
    assert detail.status_code == 200
    assert detail.json() == simulated.json()
    async with sessions() as session:
        assert await session.scalar(
            select(ValidationApproval).where(ValidationApproval.plan_id == plan_id)
        )
        runs = list(
            await session.scalars(select(ValidationRun).where(ValidationRun.plan_id == plan_id))
        )
        assert len(runs) == 1
        assert runs[0].executor == "mock_no_execution"


@pytest.mark.asyncio
async def test_plan_mutation_invalidates_approval_and_prevents_simulation(validation_api):
    client, sessions, ids = validation_api
    created = await client.post(f"/api/findings/{ids['finding']}/validation-plans", headers=auth())
    plan_id = UUID(created.json()["id"])
    assert (
        await client.post(f"/api/validation-plans/{plan_id}/approve", headers=auth())
    ).status_code == 200
    async with sessions.begin() as session:
        plan = await session.get(ValidationPlan, plan_id)
        plan.plan_json = {**plan.plan_json, "test_input": "tampered after approval"}
    result = await client.post(f"/api/validation-plans/{plan_id}/run", headers=auth())
    assert result.status_code == 409
    assert result.json()["code"] == "plan_integrity_failed"
    async with sessions() as session:
        assert (
            await session.scalar(
                select(ValidationApproval).where(ValidationApproval.plan_id == plan_id)
            )
            is None
        )
        assert (
            await session.scalar(select(ValidationRun).where(ValidationRun.plan_id == plan_id))
            is None
        )


@pytest.mark.asyncio
async def test_exact_repository_allowlist_is_required(validation_api, monkeypatch):
    client, _, ids = validation_api
    monkeypatch.setattr(settings, "validation_allowed_repositories", "octo/demo-extra")
    response = await client.post(f"/api/findings/{ids['finding']}/validation-plans", headers=auth())
    assert response.status_code == 403
    assert response.json()["code"] == "repository_not_allowed"
