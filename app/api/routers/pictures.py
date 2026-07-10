"""API de la feature Pictures (nano banana) — protégée par auth.

Flow : uploader une image de référence (R2) → POST /prompts (reverse-engineering
async, prompt sauvegardé à vie) → POST /jobs pour générer N photos de la model
(consistance visage + caractéristiques, mix outfits) → review/export.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import schemas
from app.api.deps import current_user
from app.config import get_settings
from app.db.base import get_db
from app.db.models import (
    Model,
    PictureItem,
    PictureJob,
    PicturePrompt,
    ItemStatus,
    ReviewStatus,
)
from app.services.calibration import get_calibrated_qc_rate
from app.services.estimator import estimate_pictures, max_pictures_for_budget
from app.services.pricing import load_rates
from app.workers.picture_tasks import compose_picture_job, reverse_engineer_prompt

router = APIRouter(prefix="/api/pictures", tags=["pictures"], dependencies=[Depends(current_user)])


# ---------- banque de prompts ----------


@router.post("/prompts", response_model=schemas.PicturePromptOut)
def create_prompt(payload: schemas.PicturePromptCreate, db: Session = Depends(get_db)):
    """Enregistre une image de référence et lance le reverse-engineering
    (async). Le prompt obtenu est réutilisable à vie."""
    prompt = PicturePrompt(**payload.model_dump())
    db.add(prompt)
    db.commit()
    db.refresh(prompt)
    reverse_engineer_prompt.delay(str(prompt.id))
    return prompt


@router.get("/prompts", response_model=list[schemas.PicturePromptOut])
def list_prompts(db: Session = Depends(get_db)):
    return db.scalars(select(PicturePrompt).order_by(PicturePrompt.created_at.desc())).all()


@router.delete("/prompts/{prompt_id}")
def delete_prompt(prompt_id: uuid.UUID, db: Session = Depends(get_db)):
    row = db.get(PicturePrompt, prompt_id)
    if row is None:
        raise HTTPException(404, "Prompt introuvable")
    db.delete(row)
    db.commit()
    return {"deleted": str(prompt_id)}


# ---------- estimation ----------


@router.post("/estimate", response_model=schemas.EstimateResponse)
def estimate(payload: schemas.PictureEstimateRequest, db: Session = Depends(get_db)):
    settings = get_settings()
    rates = load_rates(db)
    qc_rate = payload.qc_success_rate or get_calibrated_qc_rate(
        db, settings.default_qc_success_rate
    )
    est = estimate_pictures(payload.count, rates, model=payload.model_variant, qc_success_rate=qc_rate)
    max_pics = over = None
    if payload.budget_usd is not None:
        max_pics = max_pictures_for_budget(payload.budget_usd, rates, payload.model_variant, qc_rate)
        over = est.effective_usd > payload.budget_usd
    return schemas.EstimateResponse(
        gross_usd=est.gross_usd,
        effective_usd=est.effective_usd,
        qc_success_rate=round(qc_rate, 4),
        cost_per_delivered_video_usd=round(est.cost_per_delivered_video_usd, 4),
        max_videos_for_budget=max_pics,
        over_budget=over,
    )


# ---------- jobs ----------


@router.post("/jobs", response_model=schemas.PictureJobOut)
def create_job(payload: schemas.PictureJobCreate, db: Session = Depends(get_db)):
    """Batch de génération de photos : compose (prompt+outfit+caractéristiques),
    estime, gate le budget et dispatche vers nano banana — tout en async."""
    if db.get(Model, payload.model_id) is None:
        raise HTTPException(404, "Model introuvable")
    ready = db.scalar(
        select(PicturePrompt).where(PicturePrompt.status == "ready").limit(1)
    )
    if ready is None:
        raise HTTPException(
            409, "Aucun prompt prêt : reverse-engineerer au moins une image d'abord"
        )
    job = PictureJob(
        model_id=payload.model_id,
        count=payload.count,
        image_size=payload.image_size,
        output_format=payload.output_format,
        model_variant=payload.model_variant,
        budget_cap_usd=payload.budget_cap_usd,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    compose_picture_job.delay(str(job.id))
    return job


@router.get("/jobs", response_model=list[schemas.PictureJobOut])
def list_jobs(limit: int = 50, db: Session = Depends(get_db)):
    return db.scalars(
        select(PictureJob).order_by(PictureJob.created_at.desc()).limit(limit)
    ).all()


@router.get("/jobs/{job_id}", response_model=schemas.PictureJobOut)
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db)):
    job = db.get(PictureJob, job_id)
    if job is None:
        raise HTTPException(404, "Job introuvable")
    return job


@router.post("/items/{item_id}/review", response_model=schemas.PictureItemOut)
def review_item(item_id: uuid.UUID, payload: schemas.ReviewRequest, db: Session = Depends(get_db)):
    item = db.get(PictureItem, item_id)
    if item is None:
        raise HTTPException(404, "Item introuvable")
    if item.status != ItemStatus.DONE:
        raise HTTPException(409, f"Item non livrable (statut {item.status})")
    item.review_status = payload.decision
    db.commit()
    db.refresh(item)
    return item


@router.get("/jobs/{job_id}/export", response_model=schemas.ExportOut)
def export_job(job_id: uuid.UUID, db: Session = Depends(get_db)):
    job = db.get(PictureJob, job_id)
    if job is None:
        raise HTTPException(404, "Job introuvable")
    approved = [
        i for i in job.items
        if i.review_status == ReviewStatus.APPROVED and i.final_image_url
    ]
    return schemas.ExportOut(
        job_id=job.id,
        approved_count=len(approved),
        videos=[{"item_id": str(i.id), "url": i.final_image_url} for i in approved],
    )
