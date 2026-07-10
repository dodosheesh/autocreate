import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api import schemas
from app.api.deps import current_user
from app.api.scope import owned, tenant_query
from app.db.base import get_db
from app.db.models import Model, ModelCharacteristic, User

router = APIRouter(prefix="/api/models", tags=["models"])


@router.post("", response_model=schemas.ModelOut)
def create_model(
    payload: schemas.ModelCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    model = Model(tenant_id=user.tenant_id, **payload.model_dump())
    db.add(model)
    db.commit()
    db.refresh(model)
    return model


@router.get("", response_model=list[schemas.ModelOut])
def list_models(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return db.scalars(tenant_query(Model, user)).all()


@router.get("/{model_id}", response_model=schemas.ModelOut)
def get_model(
    model_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    return owned(db, Model, model_id, user)


@router.post("/{model_id}/characteristics", response_model=schemas.CharacteristicOut)
def add_characteristic(
    model_id: uuid.UUID,
    payload: schemas.CharacteristicCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    owned(db, Model, model_id, user)  # 404 si le model n'est pas au tenant
    charac = ModelCharacteristic(model_id=model_id, **payload.model_dump())
    db.add(charac)
    db.commit()
    db.refresh(charac)
    return charac
