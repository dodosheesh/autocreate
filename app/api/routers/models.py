import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api import schemas
from app.api.deps import current_user
from app.api.scope import owned, tenant_query
from app.db.base import get_db
from app.db.models import (
    CalibrationLog,
    GenerationJob,
    JobItem,
    Model,
    ModelCharacteristic,
    PictureItem,
    PictureJob,
    User,
)

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


@router.patch("/{model_id}", response_model=schemas.ModelOut)
def update_model(
    model_id: uuid.UUID,
    payload: schemas.ModelUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Édition partielle (ex. remplacer la photo visage cassée par un nouvel upload)."""
    model = owned(db, Model, model_id, user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(model, field, value)
    db.commit()
    db.refresh(model)
    return model


@router.delete("/{model_id}")
def delete_model(
    model_id: uuid.UUID,
    force: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Supprime un model. S'il est utilisé par des jobs :
    - sans `force` : refuse (409) en indiquant le nombre de jobs concernés ;
    - avec `force=true` : supprime AUSSI ces jobs (et leurs items/calibrations),
      après confirmation côté UI. Les médias déjà générés restent sur R2, mais
      leur suivi (jobs/items) est effacé."""
    owned(db, Model, model_id, user)

    vid_jobs = db.scalars(
        select(GenerationJob.id).where(GenerationJob.model_id == model_id)
    ).all()
    pic_jobs = db.scalars(
        select(PictureJob.id).where(PictureJob.model_id == model_id)
    ).all()
    n_jobs = len(vid_jobs) + len(pic_jobs)

    if n_jobs and not force:
        raise HTTPException(
            409,
            f"Ce model est utilisé par {n_jobs} job(s) de génération. "
            "Relance avec force=true pour les supprimer avec le model.",
        )

    # Cascade manuelle (FK sans ON DELETE) : enfants d'abord, puis les jobs,
    # puis les caractéristiques, puis le model.
    if vid_jobs:
        db.execute(delete(CalibrationLog).where(CalibrationLog.job_id.in_(vid_jobs)))
        db.execute(delete(JobItem).where(JobItem.job_id.in_(vid_jobs)))
        db.execute(delete(GenerationJob).where(GenerationJob.id.in_(vid_jobs)))
    if pic_jobs:
        db.execute(delete(PictureItem).where(PictureItem.job_id.in_(pic_jobs)))
        db.execute(delete(PictureJob).where(PictureJob.id.in_(pic_jobs)))
    db.execute(delete(ModelCharacteristic).where(ModelCharacteristic.model_id == model_id))
    db.execute(delete(Model).where(Model.id == model_id))
    db.commit()
    return {"deleted": str(model_id), "deleted_jobs": n_jobs}


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


@router.patch("/{model_id}/characteristics/{char_id}", response_model=schemas.CharacteristicOut)
def update_characteristic(
    model_id: uuid.UUID,
    char_id: uuid.UUID,
    payload: schemas.CharacteristicUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    owned(db, Model, model_id, user)  # 404 si le model n'est pas au tenant
    charac = db.get(ModelCharacteristic, char_id)
    if charac is None or charac.model_id != model_id:
        raise HTTPException(404, "Caractéristique introuvable")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(charac, field, value)
    db.commit()
    db.refresh(charac)
    return charac


@router.delete("/{model_id}/characteristics/{char_id}")
def delete_characteristic(
    model_id: uuid.UUID,
    char_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    owned(db, Model, model_id, user)  # 404 si le model n'est pas au tenant
    charac = db.get(ModelCharacteristic, char_id)
    if charac is None or charac.model_id != model_id:
        raise HTTPException(404, "Caractéristique introuvable")
    db.delete(charac)
    db.commit()
    return {"deleted": str(char_id)}
