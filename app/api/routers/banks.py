"""CRUD des banques d'assets (brief §4.3–4.8).

Le contenu (images, textes taggés, captions) est fourni par l'utilisateur ;
le moteur ne fait que le stocker et le tirer au sort à la composition.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Type

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import schemas
from app.api.deps import current_user
from app.api.scope import tenant_query
from app.db.base import get_db
from app.db.models import (
    Background,
    Caption,
    DialogueLine,
    Outfit,
    PromptTemplate,
    User,
    VoiceProfile,
)
from app.integrations.vision import describe_image as _vision_describe
from app.services.template_library import load_default_templates
from app.workers.picture_tasks import reverse_engineer_video

router = APIRouter(prefix="/api/banks", tags=["banks"])

_ASSET_MODELS = {"outfit": Outfit, "background": Background}


@router.post("/templates/load-defaults")
def load_defaults(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Charge la bibliothèque de templates prêts à l'emploi (mise en scène par
    catégorie) dans la banque du tenant. Idempotent (dédup par texte)."""
    added = load_default_templates(db, user.tenant_id)
    return {"added": added}


@router.post("/templates/reverse-video", response_model=schemas.TemplateOut)
def reverse_video(
    payload: schemas.ReverseVideoRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Reverse-engineering d'une vidéo de référence → template réutilisable.

    Crée un PromptTemplate en statut `pending` puis lance l'analyse async
    (keyframes + vision). Une fois `ready`, il est tiré dans la génération
    de sa catégorie comme n'importe quel template."""
    tmpl = PromptTemplate(
        tenant_id=user.tenant_id,
        category=payload.category,
        template_text="(analyse en cours…)",
        speaking=payload.speaking,
        status="pending",
        source_video_url=payload.source_video_url,
    )
    db.add(tmpl)
    db.commit()
    db.refresh(tmpl)
    reverse_engineer_video.delay(str(tmpl.id))
    return tmpl


def _describe_rows(kind: str, db_model, rows, suffix: str, db: Session) -> None:
    """Décrit chaque asset via la vision (appels concurrents hors session DB),
    puis écrit tags+status. Synchrone : aucun worker Celery requis. Un échec
    par image passe cette ligne en `failed` sans bloquer les autres."""
    suffix = (suffix or "").strip()
    targets = [(str(r.id), r.image_url) for r in rows]
    if not targets:
        return

    def work(target):
        rid, url = target
        try:
            desc = _vision_describe(url, kind)
            if suffix:
                desc = f"{desc} {suffix}"
            return rid, desc, None
        except Exception as exc:  # réseau/HTTP vision → cette image échoue seule
            return rid, None, str(exc)[:500]

    with ThreadPoolExecutor(max_workers=min(6, len(targets))) as pool:
        results = list(pool.map(work, targets))

    for rid, desc, err in results:
        row = db.get(db_model, uuid.UUID(rid))
        if row is None:  # supprimé entre-temps
            continue
        if desc:
            row.tags = [desc]
            row.status = "ready"
            row.error = None
        else:
            row.status = "failed"
            row.error = err  # raison visible dans l'UI (ex. image non téléchargeable)
    db.commit()  # expire_on_commit=False → les objets `rows` gardent leurs valeurs


def _bulk_describe(kind: str, db_model, payload, db, user):
    """Crée N assets puis les décrit immédiatement (vision synchrone + suffixe)."""
    created = []
    for url in payload.image_urls:
        row = db_model(tenant_id=user.tenant_id, image_url=url, tags=[], weight=payload.weight, status="pending")
        db.add(row)
        created.append(row)
    db.commit()
    for row in created:
        db.refresh(row)
    _describe_rows(kind, db_model, created, payload.suffix, db)
    return created


@router.post("/outfits/bulk-describe", response_model=list[schemas.OutfitOut])
def bulk_describe_outfits(
    payload: schemas.BulkDescribeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return _bulk_describe("outfit", Outfit, payload, db, user)


@router.post("/backgrounds/bulk-describe", response_model=list[schemas.OutfitOut])
def bulk_describe_backgrounds(
    payload: schemas.BulkDescribeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return _bulk_describe("background", Background, payload, db, user)


@router.post("/assets/describe-pending")
def describe_pending_assets(
    payload: schemas.DescribePendingRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Relance la description des outfits/backgrounds restés en `pending`
    (ex. importés avant que la description soit synchrone). Synchrone."""
    out: dict[str, int] = {}
    for kind, db_model in _ASSET_MODELS.items():
        rows = db.scalars(
            select(db_model).where(
                db_model.tenant_id == user.tenant_id, db_model.status == "pending"
            )
        ).all()
        suffix = ""
        if payload is not None:
            suffix = payload.outfit_suffix if kind == "outfit" else payload.background_suffix
        _describe_rows(kind, db_model, rows, suffix, db)
        out[kind] = len(rows)
    return {"described": out}


def _register(
    name: str,
    db_model: Type,
    create_schema: Type[BaseModel],
    out_schema: Type[BaseModel],
) -> None:
    @router.post(f"/{name}", response_model=out_schema, name=f"create_{name}")
    def create(
        payload: create_schema,  # type: ignore[valid-type]
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
    ):
        row = db_model(tenant_id=user.tenant_id, **payload.model_dump())
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @router.get(f"/{name}", response_model=list[out_schema], name=f"list_{name}")
    def list_all(
        category: str | None = None,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
    ):
        query = tenant_query(db_model, user)
        if category is not None and hasattr(db_model, "category"):
            query = query.where(db_model.category == category)
        return db.scalars(query).all()

    @router.delete(f"/{name}/{{row_id}}", name=f"delete_{name}")
    def delete(
        row_id: uuid.UUID,
        db: Session = Depends(get_db),
        user: User = Depends(current_user),
    ):
        row = db.get(db_model, row_id)
        if row is None or row.tenant_id != user.tenant_id:
            raise HTTPException(404, f"{name} : entrée introuvable")
        db.delete(row)
        db.commit()
        return {"deleted": str(row_id)}


_register("outfits", Outfit, schemas.OutfitCreate, schemas.OutfitOut)
_register("backgrounds", Background, schemas.BackgroundCreate, schemas.BackgroundOut)
_register("templates", PromptTemplate, schemas.TemplateCreate, schemas.TemplateOut)
_register("dialogues", DialogueLine, schemas.DialogueLineCreate, schemas.DialogueLineOut)
_register("captions", Caption, schemas.CaptionCreate, schemas.CaptionOut)
_register("voices", VoiceProfile, schemas.VoiceProfileCreate, schemas.VoiceProfileOut)
