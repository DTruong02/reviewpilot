import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import verify_github_signature
from app.db.session import get_session
from app.services.webhook_ingestion import ingest_delivery
from app.workers.queue import dispatch_queued_review

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])
MAX_WEBHOOK_BYTES = 1_000_000


@router.post("/github", status_code=202)
async def github_webhook(
    request: Request,
    response: Response,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool | str]:
    body_buffer = bytearray()
    async for chunk in request.stream():
        body_buffer.extend(chunk)
        if len(body_buffer) > MAX_WEBHOOK_BYTES:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "payload_too_large",
                    "message": "Webhook payload exceeds the size limit.",
                },
            )
    body = bytes(body_buffer)
    if not verify_github_signature(body, x_hub_signature_256, settings.github_webhook_secret):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "invalid_signature",
                "message": "Webhook signature is missing or invalid.",
            },
        )
    if not x_github_delivery or len(x_github_delivery) > 255 or not x_github_event:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_headers",
                "message": "Required GitHub delivery headers are missing or invalid.",
            },
        )
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_payload", "message": "Webhook payload must be a JSON object."},
        ) from None

    try:
        result = await ingest_delivery(session, x_github_delivery, x_github_event, payload)
    except ValidationError:
        await session.rollback()
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_payload",
                "message": "Pull request event is missing required fields.",
            },
        ) from None
    except IntegrityError:
        await session.rollback()
        # The unique delivery ID is the idempotency boundary for concurrent redeliveries.
        from sqlalchemy import select

        from app.models.entities import WebhookDelivery

        duplicate = (
            await session.scalar(
                select(WebhookDelivery.id).where(
                    WebhookDelivery.github_delivery_id == x_github_delivery
                )
            )
            is not None
        )
        if not duplicate:
            raise HTTPException(
                status_code=409,
                detail={"code": "ingestion_conflict", "message": "Webhook could not be persisted."},
            ) from None
        accepted = False
        result = None
    if result is not None:
        duplicate, accepted = result.duplicate, result.accepted
        if result.review_id is not None:
            # The DB/outbox commit has already happened; a broker error is recovered by beat.
            try:
                await dispatch_queued_review(session, result.review_id)
            except Exception:
                pass
    response.headers["X-ReviewPilot-Duplicate"] = str(duplicate).lower()
    return {"received": True, "duplicate": duplicate, "accepted": accepted}
