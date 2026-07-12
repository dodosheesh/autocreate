"""API de la feature Pictures (nano banana) — protégée par auth.

Flow : uploader une image de référence (R2) → POST /prompts (reverse-engineering
async, prompt sauvegardé à vie) → POST /jobs pour générer N photos de la model
(consistance visage + caractéristiques, mix outfits) → review/export.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import schemas
from app.api.deps import current_user
from app.api.scope import owned, tenant_query
from app.config import get_settings
from app.db.base import get_db
from app.db.models import (
    Model,
    PictureItem,
    PictureJob,
    PicturePrompt,
    PromptStatus,
    ItemStatus,
    ReviewStatus,
    User,
)
from app.integrations.vision import reverse_engineer_prompt as _vision_reverse
from app.services.calibration import get_calibrated_qc_rate
from app.services.estimator import estimate_pictures, max_pictures_for_budget
from app.services.export_zip import build_media_zip
from app.services.pricing import load_rates
from app.workers.picture_tasks import compose_picture_job, recheck_picture_job

router = APIRouter(prefix="/api/pictures", tags=["pictures"])


# ---------- banque de prompts ----------


@router.post("/prompts", response_model=schemas.PicturePromptOut)
def create_prompt(
    payload: schemas.PicturePromptCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Enregistre une image de référence et la reverse-engineere en prompt
    réutilisable (SYNCHRONE : aucun worker requis, résultat immédiat)."""
    prompt = PicturePrompt(tenant_id=user.tenant_id, **payload.model_dump())
    db.add(prompt)
    db.commit()
    db.refresh(prompt)
    try:
        prompt.prompt_text = _vision_reverse(prompt.source_image_url, None)
        prompt.status = PromptStatus.READY
        prompt.error = None
    except Exception as exc:  # vision/HTTP → prompt en échec, raison visible
        prompt.status = PromptStatus.FAILED
        prompt.error = str(exc)[:2000]
    db.commit()
    db.refresh(prompt)
    return prompt


@router.post("/prompts/bulk", response_model=list[schemas.PicturePromptOut])
def create_prompts_bulk(
    payload: schemas.BulkPicturePromptRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Reverse-engineering en MASSE : enregistre N images et les analyse en
    parallèle (vision synchrone, aucun worker requis). Une image qui échoue
    passe en `failed` sans bloquer les autres."""
    from concurrent.futures import ThreadPoolExecutor

    created = [
        PicturePrompt(tenant_id=user.tenant_id, source_image_url=url,
                      tags=payload.tags, weight=payload.weight, status=PromptStatus.PENDING)
        for url in payload.source_image_urls
    ]
    db.add_all(created)
    db.commit()
    for row in created:
        db.refresh(row)

    def work(row):
        try:
            return str(row.id), _vision_reverse(row.source_image_url, None), None
        except Exception as exc:  # vision/HTTP → cette image échoue seule
            return str(row.id), None, str(exc)[:2000]

    with ThreadPoolExecutor(max_workers=min(6, len(created))) as pool:
        results = list(pool.map(work, created))

    for rid, text, err in results:
        row = db.get(PicturePrompt, uuid.UUID(rid))
        if row is None:
            continue
        if text:
            row.prompt_text = text
            row.status = PromptStatus.READY
            row.error = None
        else:
            row.status = PromptStatus.FAILED
            row.error = err
    db.commit()
    for row in created:
        db.refresh(row)
    return created


@router.get("/prompts", response_model=list[schemas.PicturePromptOut])
def list_prompts(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return db.scalars(
        tenant_query(PicturePrompt, user).order_by(PicturePrompt.created_at.desc())
    ).all()


@router.delete("/prompts/{prompt_id}")
def delete_prompt(
    prompt_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    row = owned(db, PicturePrompt, prompt_id, user)
    db.delete(row)
    db.commit()
    return {"deleted": str(prompt_id)}


# ---------- estimation ----------


@router.post("/estimate", response_model=schemas.EstimateResponse)
def estimate(
    payload: schemas.PictureEstimateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    settings = get_settings()
    rates = load_rates(db)
    qc_rate = payload.qc_success_rate or get_calibrated_qc_rate(
        db, settings.default_qc_success_rate
    )
    est = estimate_pictures(payload.count, rates, model=payload.model_variant,
                            qc_success_rate=qc_rate, resolution=payload.image_resolution)
    max_pics = over = None
    if payload.budget_usd is not None:
        max_pics = max_pictures_for_budget(payload.budget_usd, rates, payload.model_variant,
                                           qc_rate, resolution=payload.image_resolution)
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
def create_job(
    payload: schemas.PictureJobCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Batch de génération de photos : compose (prompt+outfit+caractéristiques),
    estime, gate le budget et dispatche vers nano banana — tout en async."""
    owned(db, Model, payload.model_id, user)
    ready = db.scalar(
        tenant_query(PicturePrompt, user).where(PicturePrompt.status == "ready").limit(1)
    )
    if ready is None:
        raise HTTPException(
            409, "Aucun prompt prêt : reverse-engineerer au moins une image d'abord"
        )
    job = PictureJob(
        tenant_id=user.tenant_id,
        model_id=payload.model_id,
        count=payload.count,
        image_size=payload.image_size,
        image_resolution=payload.image_resolution,
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
def list_jobs(
    limit: int = 50, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    return db.scalars(
        tenant_query(PictureJob, user).order_by(PictureJob.created_at.desc()).limit(limit)
    ).all()


@router.get("/jobs/{job_id}", response_model=schemas.PictureJobOut)
def get_job(
    job_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    return owned(db, PictureJob, job_id, user)


@router.post("/jobs/{job_id}/recheck", response_model=schemas.PictureJobOut)
def recheck_job(
    job_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Interroge kie.ai à la demande pour les items dispatchés (si un webhook
    s'est perdu). Ne dépend ni du webhook ni de celery beat."""
    job = owned(db, PictureJob, job_id, user)
    recheck_picture_job(str(job_id))
    db.refresh(job)
    return job


@router.post("/items/{item_id}/review", response_model=schemas.PictureItemOut)
def review_item(
    item_id: uuid.UUID,
    payload: schemas.ReviewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    item = db.get(PictureItem, item_id)
    if item is None or item.job.tenant_id != user.tenant_id:
        raise HTTPException(404, "Item introuvable")
    if item.status != ItemStatus.DONE:
        raise HTTPException(409, f"Item non livrable (statut {item.status})")
    item.review_status = payload.decision
    db.commit()
    db.refresh(item)
    return item


@router.post("/jobs/{job_id}/approve-all", response_model=schemas.PictureJobOut)
def approve_all(
    job_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Approuve d'un coup toutes les photos livrées (statut DONE) du job."""
    job = owned(db, PictureJob, job_id, user)
    for item in job.items:
        if item.status == ItemStatus.DONE:
            item.review_status = ReviewStatus.APPROVED
    db.commit()
    db.refresh(job)
    return job


@router.get("/jobs/{job_id}/export", response_model=schemas.ExportOut)
def export_job(
    job_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    job = owned(db, PictureJob, job_id, user)
    approved = [
        i for i in job.items
        if i.review_status == ReviewStatus.APPROVED and i.final_image_url
    ]
    return schemas.ExportOut(
        job_id=job.id,
        approved_count=len(approved),
        videos=[{"item_id": str(i.id), "url": i.final_image_url} for i in approved],
    )


@router.get("/jobs/{job_id}/export.zip")
def export_job_zip(
    job_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """ZIP de toutes les photos approuvées du job (fichiers + manifest.json)."""
    job = owned(db, PictureJob, job_id, user)
    approved = [
        i for i in job.items
        if i.review_status == ReviewStatus.APPROVED and i.final_image_url
    ]
    if not approved:
        raise HTTPException(409, "Aucune photo approuvée à exporter")
    entries = [
        {"item_id": i.id, "url": i.final_image_url, "prompt": i.filled_prompt}
        for i in approved
    ]
    data = build_media_zip(entries, default_ext="png")
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="photos_{str(job_id)[:8]}.zip"'},
    )
