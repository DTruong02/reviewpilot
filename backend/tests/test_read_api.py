import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
)


@pytest.fixture
async def api():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    async with sessions() as session:
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
        pull_request = PullRequest(
            repository_id=repository.id,
            github_pr_number=7,
            title="Fix result",
            state="open",
            head_sha="a" * 40,
            html_url="https://github.com/octo/demo/pull/7",
        )
        session.add(pull_request)
        await session.flush()
        old_review = Review(
            pull_request_id=pull_request.id,
            commit_sha="b" * 40,
            status=ReviewStatus.completed,
            model_name="fake",
            created_at=datetime.now(UTC) - timedelta(minutes=1),
            duration_ms=25,
        )
        new_review = Review(
            pull_request_id=pull_request.id,
            commit_sha="a" * 40,
            status=ReviewStatus.failed,
            error_code="private exception details should not be exposed",
            created_at=datetime.now(UTC),
        )
        session.add_all([old_review, new_review])
        await session.flush()
        finding = Finding(
            review_id=old_review.id,
            file_path="src/main.py",
            line_number=12,
            severity=Severity.high,
            category=Category.bug,
            title="Returns the wrong value",
            explanation="The changed branch returns early.",
            suggested_fix="Return the calculated value.",
        )
        low_finding = Finding(
            review_id=old_review.id,
            file_path="src/z.py",
            line_number=3,
            severity=Severity.low,
            category=Category.maintainability,
            title="Could simplify",
            explanation="Minor maintenance observation.",
        )
        session.add_all([finding, low_finding])
        await session.commit()
        ids = {
            "repository": repository.id,
            "pull_request": pull_request.id,
            "old_review": old_review.id,
            "new_review": new_review.id,
            "finding": finding.id,
        }

    yield sessions, ids
    app.dependency_overrides.clear()
    await engine.dispose()


def client_for(client_address: tuple[str, int] = ("127.0.0.1", 12345)) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, client=client_address),
        base_url="http://test",
    )


@pytest.mark.asyncio
async def test_local_repositories_and_paginated_pull_requests(api):
    _, ids = api
    async with client_for() as client:
        repositories = await client.get("/api/repositories?limit=10&offset=0")
        prs = await client.get(f"/api/repositories/{ids['repository']}/pull-requests?limit=1")
    assert repositories.status_code == 200
    assert repositories.json()["items"][0]["full_name"] == "octo/demo"
    assert repositories.json()["total"] == 1
    assert prs.status_code == 200
    assert prs.json()["total"] == 1
    assert prs.json()["items"][0]["latest_review_status"] == "failed"


@pytest.mark.asyncio
async def test_pull_request_detail_and_review_history(api):
    _, ids = api
    async with client_for() as client:
        detail = await client.get(f"/api/pull-requests/{ids['pull_request']}")
        history = await client.get(f"/api/pull-requests/{ids['pull_request']}/reviews?limit=1")
    assert detail.status_code == 200
    assert detail.json()["repository_full_name"] == "octo/demo"
    assert detail.json()["latest_review_status"] == "failed"
    assert history.status_code == 200
    assert history.json()["total"] == 2
    assert len(history.json()["items"]) == 1
    assert history.json()["items"][0]["status"] == "failed"
    assert history.json()["items"][0]["error_code"] == "review_failed"
    assert "private exception" not in json.dumps(history.json())


@pytest.mark.asyncio
async def test_review_detail_and_feedback_update(api):
    _, ids = api
    async with client_for() as client:
        review = await client.get(f"/api/reviews/{ids['old_review']}")
        feedback = await client.patch(
            f"/api/findings/{ids['finding']}/feedback", json={"feedback": "helpful"}
        )
    assert review.status_code == 200
    assert review.json()["findings"][0]["file_path"] == "src/main.py"
    assert review.json()["findings"][0]["severity"] == "high"
    assert review.json()["findings"][1]["severity"] == "low"
    assert feedback.status_code == 200
    assert feedback.json() == {"id": str(ids["finding"]), "feedback": "helpful"}
    async with api[0]() as session:
        finding = await session.get(Finding, ids["finding"])
        assert finding.feedback == "helpful"


@pytest.mark.asyncio
async def test_review_detail_exposes_source_free_context_provenance(api):
    sessions, ids = api
    async with sessions.begin() as session:
        review = await session.get(Review, ids["old_review"])
        review.context_mode = "context_aware"
        review.context_metadata = [
            {
                "file_path": "src/helper.py",
                "symbol_id": "symbol-123",
                "start_line": 4,
                "end_line": 8,
                "commit_sha": "b" * 40,
                "retrieval_reason": "imported_file",
                "score": 0.75,
            }
        ]
        finding = await session.get(Finding, ids["finding"])
        finding.evidence_refs = [
            {
                "symbol_id": "symbol-123",
                "file_path": "src/helper.py",
                "start_line": 4,
                "end_line": 8,
            }
        ]

    async with client_for() as client:
        response = await client.get(f"/api/reviews/{ids['old_review']}")

    assert response.status_code == 200
    review_json = response.json()
    assert review_json["context_mode"] == "context_aware"
    assert review_json["context_metadata"][0]["retrieval_reason"] == "imported_file"
    assert "snippet" not in review_json["context_metadata"][0]
    assert review_json["findings"][0]["evidence_refs"][0]["symbol_id"] == "symbol-123"


@pytest.mark.asyncio
async def test_feedback_rejects_values_outside_the_contract(api):
    _, ids = api
    async with client_for() as client:
        response = await client.patch(
            f"/api/findings/{ids['finding']}/feedback", json={"feedback": "approved"}
        )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_missing_resources_and_pagination_bounds_are_safe(api):
    _, ids = api
    missing_id = uuid.uuid4()
    async with client_for() as client:
        missing = await client.get(f"/api/reviews/{missing_id}")
        missing_repo = await client.get(f"/api/repositories/{missing_id}/pull-requests")
        oversized = await client.get("/api/repositories?limit=101")
    assert missing.status_code == 404
    assert missing.json()["code"] == "review_not_found"
    assert missing_repo.status_code == 404
    assert oversized.status_code == 422


@pytest.mark.asyncio
async def test_remote_api_is_rejected_even_with_spoofed_forwarded_for(api):
    async with client_for(("203.0.113.9", 4567)) as client:
        response = await client.get("/api/repositories", headers={"X-Forwarded-For": "127.0.0.1"})
    assert response.status_code == 403
    assert response.json()["code"] == "local_access_only"


@pytest.mark.asyncio
async def test_remote_webhook_route_is_not_blocked_by_local_only_guard(api):
    async with client_for(("203.0.113.9", 4567)) as client:
        response = await client.post(
            "/api/webhooks/github",
            content=b"{}",
            headers={
                "X-Hub-Signature-256": "sha256=invalid",
                "X-GitHub-Delivery": str(uuid.uuid4()),
                "X-GitHub-Event": "pull_request",
            },
        )
    assert response.status_code == 401
