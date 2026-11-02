from celery import Celery
from celery.schedules import schedule

from app.core.config import settings

celery_app = Celery(
    "reviewpilot",
    broker=settings.redis_url,
    include=["app.workers.tasks", "app.workers.index_tasks"],
)
celery_app.conf.update(
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_acks_on_failure_or_timeout=True,
    task_reject_on_worker_lost=False,
    worker_prefetch_multiplier=1,
    task_publish_retry=False,
    broker_connection_retry_on_startup=False,
    broker_connection_timeout=2,
    broker_transport_options={"socket_connect_timeout": 2, "socket_timeout": 2},
    task_soft_time_limit=840,
    task_time_limit=900,
    beat_schedule={
        "review-queue-reconciliation": {
            "task": "reviewpilot.reconcile_queued_reviews",
            "schedule": schedule(60.0),
        },
        "repository-index-reconciliation": {
            "task": "reviewpilot.reconcile_repository_indexes",
            "schedule": schedule(60.0),
        },
        "repository-index-retention": {
            "task": "reviewpilot.cleanup_old_repository_indexes",
            "schedule": schedule(86_400.0),
        },
    },
)
