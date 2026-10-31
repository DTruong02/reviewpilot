from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Installation, PullRequest, Repository, WebhookDelivery
from app.schemas.webhook import PullRequestEvent

SUPPORTED_ACTIONS = {"opened", "reopened", "synchronize"}


async def ingest_delivery(
    session: AsyncSession, delivery_id: str, event_type: str, payload: dict
) -> tuple[bool, bool]:
    """Persist a delivery and its PR metadata atomically. Returns (duplicate, accepted)."""
    existing = await session.scalar(
        select(WebhookDelivery.id).where(WebhookDelivery.github_delivery_id == delivery_id)
    )
    if existing:
        return True, False

    action = payload.get("action") if isinstance(payload, dict) else None
    delivery = WebhookDelivery(
        github_delivery_id=delivery_id,
        event_type=event_type,
        action=action if isinstance(action, str) else None,
        processing_status="received",
    )
    session.add(delivery)

    if event_type != "pull_request" or action not in SUPPORTED_ACTIONS:
        delivery.processing_status = "processed"
        await session.commit()
        return False, False

    event = PullRequestEvent.model_validate(payload)
    owner, name = event.repository.full_name.split("/", maxsplit=1)

    installation = await session.scalar(
        select(Installation).where(Installation.github_installation_id == event.installation.id)
    )
    if installation is None:
        installation = Installation(github_installation_id=event.installation.id)
        session.add(installation)
        await session.flush()
    installation.account_login = (
        event.installation.account.login if event.installation.account else None
    )

    repository = await session.scalar(
        select(Repository).where(Repository.github_repository_id == event.repository.id)
    )
    if repository is None:
        repository = Repository(
            installation_id=installation.id,
            github_repository_id=event.repository.id,
            owner=owner,
            name=name,
            full_name=event.repository.full_name,
            private=event.repository.private,
        )
        session.add(repository)
        await session.flush()
    else:
        repository.installation_id = installation.id
        repository.owner = owner
        repository.name = name
        repository.full_name = event.repository.full_name
        repository.private = event.repository.private

    pull_request = await session.scalar(
        select(PullRequest).where(
            PullRequest.repository_id == repository.id,
            PullRequest.github_pr_number == event.pull_request.number,
        )
    )
    if pull_request is None:
        pull_request = PullRequest(
            repository_id=repository.id,
            github_pr_number=event.pull_request.number,
            title=event.pull_request.title,
            state=event.pull_request.state,
            head_sha=event.pull_request.head.sha,
            html_url=event.pull_request.html_url,
        )
        session.add(pull_request)
    else:
        pull_request.title = event.pull_request.title
        pull_request.state = event.pull_request.state
        pull_request.head_sha = event.pull_request.head.sha
        pull_request.html_url = event.pull_request.html_url

    delivery.processing_status = "processed"
    await session.commit()
    return False, True
