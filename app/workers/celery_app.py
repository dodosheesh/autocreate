from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "autocreate",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_retry_delay=30,
    broker_connection_retry_on_startup=True,
    # Filet de sécurité : les webhooks kie.ai restent le chemin nominal,
    # le polling rattrape un callback perdu (item bloqué en dispatched)
    beat_schedule={
        "poll-pending-items": {
            "task": "app.workers.tasks.poll_pending_items",
            "schedule": 120.0,
        },
    },
)
