"""CRUD des banques d'assets (brief §4.3–4.8).

Le contenu (images, textes taggés, captions) est fourni par l'utilisateur ;
le moteur ne fait que le stocker et le tirer au sort à la composition.
"""

import uuid
from typing import Type

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
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
from app.services.template_library import load_default_templates
from app.workers.picture_tasks import reverse_engineer_video

router = APIRouter(prefix="/api/banks", tags=["banks"])


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
