"""Diagnostic système : état worker/broker + repérage des URLs d'images cassées
(placeholder R2 laissé après un mauvais R2_PUBLIC_BASE_URL). Protégé par auth.

But : donner à l'utilisateur une réponse claire quand une génération reste
« pending » (worker/broker) ou échoue (images de référence non téléchargeables)."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db.base import get_db
from app.db.models import (
    Background,
    Model,
    ModelCharacteristic,
    Outfit,
    PicturePrompt,
    User,
)
from app.workers.celery_app import celery_app

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])

# Jetons trahissant une URL publique R2 laissée en placeholder (donc non
# téléchargeable par kie.ai / la vision).
_PLACEHOLDER_TOKENS = ("remplacer", "xxxx", "replace", "example", "ton-", "your-", "pub-xxxx")


def _is_placeholder(url: str | None) -> bool:
    u = (url or "").lower()
    return (not u) or any(tok in u for tok in _PLACEHOLDER_TOKENS)


def _broker_ok() -> bool:
    try:
        conn = celery_app.connection()
        conn.ensure_connection(max_retries=1, timeout=2)
        conn.release()
        return True
    except Exception:
        return False


def _workers() -> list[str]:
    try:
        replies = celery_app.control.ping(timeout=2) or []
        return [name for reply in replies for name in reply.keys()]
    except Exception:
        return []


@router.get("")
def diagnostics(db: Session = Depends(get_db), user: User = Depends(current_user)):
    tid = user.tenant_id

    # --- images de référence cassées (URL placeholder en base) ---
    broken: dict[str, int] = {}

    faces = db.scalars(select(Model.face_reference_url).where(Model.tenant_id == tid)).all()
    broken["model_faces"] = sum(_is_placeholder(u) for u in faces)

    char_urls = db.scalars(
        select(ModelCharacteristic.reference_image_url)
        .join(Model, ModelCharacteristic.model_id == Model.id)
        .where(Model.tenant_id == tid)
    ).all()
    broken["characteristics"] = sum(_is_placeholder(u) for u in char_urls)

    for label, model, col in (
        ("outfits", Outfit, Outfit.image_url),
        ("backgrounds", Background, Background.image_url),
        ("picture_prompts", PicturePrompt, PicturePrompt.source_image_url),
    ):
        urls = db.scalars(select(col).where(model.tenant_id == tid)).all()
        broken[label] = sum(_is_placeholder(u) for u in urls)

    workers = _workers()
    return {
        "broker_reachable": _broker_ok(),
        "workers_online": len(workers),
        "worker_names": workers,
        "broken_reference_urls": broken,
        "broken_total": sum(broken.values()),
    }
