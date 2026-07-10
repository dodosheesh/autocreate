"""Éditeur de tarifs — la table pricing est la source de vérité de l'estimateur.

kie.ai bloque le scraping, donc les seuls tarifs 100 % exacts sont ceux du
compte de l'utilisateur : on lui permet de les saisir/corriger ici. La table
est globale (config partagée), pas scopée par tenant.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db.base import get_db
from app.db.models import Pricing

router = APIRouter(prefix="/api/pricing", tags=["pricing"], dependencies=[Depends(current_user)])


class PricingOut(BaseModel):
    id: uuid.UUID
    model: str
    resolution: str | None
    with_ref: bool | None
    unit: str
    rate_usd: float

    model_config = {"from_attributes": True}


class PricingUpdate(BaseModel):
    rate_usd: float = Field(ge=0)


class PricingCreate(BaseModel):
    model: str
    resolution: str | None = None
    with_ref: bool | None = None
    unit: str = "per_sec"
    rate_usd: float = Field(ge=0)


@router.get("", response_model=list[PricingOut])
def list_pricing(db: Session = Depends(get_db)):
    return db.scalars(select(Pricing).order_by(Pricing.model, Pricing.resolution)).all()


@router.put("/{pricing_id}", response_model=PricingOut)
def update_pricing(pricing_id: uuid.UUID, payload: PricingUpdate, db: Session = Depends(get_db)):
    row = db.get(Pricing, pricing_id)
    if row is None:
        raise HTTPException(404, "Tarif introuvable")
    row.rate_usd = payload.rate_usd
    db.commit()
    db.refresh(row)
    return row


@router.post("", response_model=PricingOut)
def create_pricing(payload: PricingCreate, db: Session = Depends(get_db)):
    """Ajoute une ligne tarifaire (ex : nouveau modèle / résolution). Upsert
    sur la clé (model, resolution, with_ref, unit)."""
    existing = db.scalar(
        select(Pricing).where(
            Pricing.model == payload.model,
            Pricing.resolution == payload.resolution,
            Pricing.with_ref == payload.with_ref,
            Pricing.unit == payload.unit,
        )
    )
    if existing:
        existing.rate_usd = payload.rate_usd
        db.commit()
        db.refresh(existing)
        return existing
    row = Pricing(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
