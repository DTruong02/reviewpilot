import hashlib
import hmac
import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.routes import webhooks
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models.entities import Installation, PullRequest, WebhookDelivery

SECRET = "test-webhook-secret"
SHA1 = "a" * 40
SHA2 = "b" * 40


@pytest.fixture
async def client():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    previous_secret = webhooks.settings.github_webhook_secret
    webhooks.settings.github_webhook_secret = SECRET
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        yield test_client, sessions
    webhooks.settings.github_webhook_secret = previous_secret
    app.dependency_overrides.clear()
    await engine.dispose()


def payload(sha: str = SHA1, action: str = "opened") -> dict:
    return {
        "action": action,
        "installation": {"id": 71, "account": {"login": "octo-org"}},
        "repository": {"id": 99, "name": "demo", "full_name": "octo-org/demo", "private": True},
        "pull_request": {
            "number": 12,
            "title": "Fix issue",
            "state": "open",
            "html_url": "https://github.com/octo-org/demo/pull/12",
            "head": {"sha": sha},
        },
    }


def signature(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


async def send(
    client, body: bytes, delivery_id: str, event: str = "pull_request", sig: str | None = None
):
    return await client.post(
        "/api/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": sig or signature(body),
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": event,
            "Content-Type": "application/json",
        },
    )


@pytest.mark.asyncio
async def test_signature_is_checked_before_payload_processing(client):
    api, _ = client
    response = await send(api, b"not-json", str(uuid.uuid4()), sig="sha256=bad")
    assert response.status_code == 401
    assert response.json() == {
        "code": "invalid_signature",
        "message": "Webhook signature is missing or invalid.",
    }


@pytest.mark.asyncio
async def test_signed_pr_delivery_persists_once_and_duplicate_is_ignored(client):
    api, sessions = client
    body = json.dumps(payload()).encode()
    delivery_id = str(uuid.uuid4())
    first = await send(api, body, delivery_id)
    second = await send(api, body, delivery_id)
    assert first.status_code == second.status_code == 202
    assert first.json()["accepted"] is True
    assert second.json()["duplicate"] is True
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Installation)) == 1
        assert await session.scalar(select(func.count()).select_from(PullRequest)) == 1
        assert await session.scalar(select(func.count()).select_from(WebhookDelivery)) == 1


@pytest.mark.asyncio
async def test_unsupported_event_is_recorded_without_upserting_pr(client):
    api, sessions = client
    body = json.dumps({"action": "created"}).encode()
    response = await send(api, body, str(uuid.uuid4()), event="issues")
    assert response.status_code == 202
    assert response.json()["accepted"] is False
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(WebhookDelivery)) == 1
        assert await session.scalar(select(func.count()).select_from(Installation)) == 0


@pytest.mark.asyncio
async def test_new_delivery_updates_pull_request_head_and_title(client):
    api, sessions = client
    for event in (payload(), payload(SHA2, action="synchronize")):
        body = json.dumps(event).encode()
        response = await send(api, body, str(uuid.uuid4()))
        assert response.status_code == 202
    async with sessions() as session:
        pull_request = await session.scalar(select(PullRequest))
        assert pull_request.head_sha == SHA2
        assert await session.scalar(select(func.count()).select_from(PullRequest)) == 1


@pytest.mark.asyncio
async def test_database_rejects_duplicate_github_installation_id(client):
    _, sessions = client
    async with sessions() as session:
        session.add_all(
            [Installation(github_installation_id=501), Installation(github_installation_id=501)]
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


@pytest.mark.asyncio
async def test_health_does_not_require_database():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        response = await api.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
