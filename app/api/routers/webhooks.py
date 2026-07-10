import hmac

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import get_db
from app.db.models import JobItem, PictureItem
from app.integrations.kie import parse_task_payload
from app.workers.picture_tasks import apply_kie_picture_result
from app.workers.tasks import apply_kie_result

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.post("/kie")
async def kie_callback(request: Request, secret: str = "", db: Session = Depends(get_db)):
    """Callback kie.ai à la complétion d'une tâche (structure = recordInfo).

    Authentifié par secret partagé (`?secret=…`) plutôt que par cookie —
    l'appelant est kie.ai, pas un navigateur. Le callBackUrl envoyé à kie.ai
    embarque ce secret. Toujours répondre 200 sur un payload authentifié pour
    éviter les re-livraisons en boucle ; `poll_pending_items` est le filet.
    """
    expected = get_settings().kie_webhook_secret
    if not expected:
        # Pas de secret configuré → on refuse plutôt que d'exposer l'endpoint
        # (sinon n'importe qui peut injecter des résultats de jobs / URLs).
        raise HTTPException(503, "Webhook non configuré (KIE_WEBHOOK_SECRET requis)")
    if not hmac.compare_digest(secret, expected):
        raise HTTPException(401, "Secret webhook invalide")
    body = await request.json()
    result = parse_task_payload(body.get("data", body))
    if not result.task_id:
        return {"ok": False, "reason": "taskId manquant"}
    # Le même endpoint sert vidéo (JobItem) et image (PictureItem) — on route
    # selon la table qui porte ce task_id.
    video_item = db.scalar(select(JobItem).where(JobItem.seedance_task_id == result.task_id))
    if video_item is not None:
        apply_kie_result(str(video_item.id), result)
        return {"ok": True, "kind": "video"}
    picture_item = db.scalar(select(PictureItem).where(PictureItem.kie_task_id == result.task_id))
    if picture_item is not None:
        apply_kie_picture_result(str(picture_item.id), result)
        return {"ok": True, "kind": "picture"}
    return {"ok": False, "reason": "item inconnu"}
