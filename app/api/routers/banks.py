"""CRUD des banques d'assets (brief §4.3–4.8).

Le contenu (images, textes taggés, captions) est fourni par l'utilisateur ;
le moteur ne fait que le stocker et le tirer au sort à la composition.
"""

import uuid
from typing import Type

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import schemas
from app.db.base import get_db
from app.db.models import (
    Background,
    Caption,
    DialogueLine,
    Outfit,
    PromptTemplate,
    VoiceProfile,
)

router = APIRouter(prefix="/api/banks", tags=["banks"])


def _register(
    name: str,
    db_model: Type,
    create_schema: Type[BaseModel],
    out_schema: Type[BaseModel],
) -> None:
    @router.post(f"/{name}", response_model=out_schema, name=f"create_{name}")
    def create(payload: create_schema, db: Session = Depends(get_db)):  # type: ignore[valid-type]
        row = db_model(**payload.model_dump())
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @router.get(f"/{name}", response_model=list[out_schema], name=f"list_{name}")
    def list_all(category: str | None = None, db: Session = Depends(get_db)):
        query = select(db_model)
        if category is not None and hasattr(db_model, "category"):
            query = query.where(db_model.category == category)
        return db.scalars(query).all()

    @router.delete(f"/{name}/{{row_id}}", name=f"delete_{name}")
    def delete(row_id: uuid.UUID, db: Session = Depends(get_db)):
        row = db.get(db_model, row_id)
        if row is None:
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
