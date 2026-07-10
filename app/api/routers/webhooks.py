import hmac

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import get_db
from app.db.models import JobItem
from app.integrations.kie import parse_task_payload
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
    if expected and not hmac.compare_digest(secret, expected):
        raise HTTPException(401, "Secret webhook invalide")
    body = await request.json()
    result = parse_task_payload(body.get("data", body))
    if not result.task_id:
        return {"ok": False, "reason": "taskId manquant"}
    item = db.scalar(select(JobItem).where(JobItem.seedance_task_id == result.task_id))
    if item is None:
        return {"ok": False, "reason": "item inconnu"}
    apply_kie_result(str(item.id), result)
    return {"ok": True}
