import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import schemas
from app.db.base import get_db
from app.db.models import Model, ModelCharacteristic

router = APIRouter(prefix="/api/models", tags=["models"])


@router.post("", response_model=schemas.ModelOut)
def create_model(payload: schemas.ModelCreate, db: Session = Depends(get_db)):
    model = Model(**payload.model_dump())
    db.add(model)
    db.commit()
    db.refresh(model)
    return model


@router.get("", response_model=list[schemas.ModelOut])
def list_models(db: Session = Depends(get_db)):
    return db.scalars(select(Model)).all()


@router.get("/{model_id}", response_model=schemas.ModelOut)
def get_model(model_id: uuid.UUID, db: Session = Depends(get_db)):
    model = db.get(Model, model_id)
    if model is None:
        raise HTTPException(404, "Model introuvable")
    return model


@router.post("/{model_id}/characteristics", response_model=schemas.CharacteristicOut)
def add_characteristic(
    model_id: uuid.UUID, payload: schemas.CharacteristicCreate, db: Session = Depends(get_db)
):
    if db.get(Model, model_id) is None:
        raise HTTPException(404, "Model introuvable")
    charac = ModelCharacteristic(model_id=model_id, **payload.model_dump())
    db.add(charac)
    db.commit()
    db.refresh(charac)
    return charac
