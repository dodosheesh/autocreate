import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api import schemas
from app.config import get_settings
from app.db.base import get_db
from app.db.models import GenerationJob, JobItem, JobStatus, Model
from app.services import composer
from app.services.calibration import get_calibrated_qc_rate
from app.services.estimator import ItemSpec, estimate_batch, max_videos_for_budget
from app.services.pricing import load_rates
from app.workers.tasks import compose_job, dispatch_seedance

router = APIRouter(prefix="/api", tags=["jobs"])


@router.post("/jobs/batch", response_model=schemas.JobOut)
def create_batch_job(payload: schemas.BatchJobCreate, db: Session = Depends(get_db)):
    """Job batch Phase 2 : le moteur compose N variantes dédupliquées par
    catégorie depuis les banques (templates, outfits, backgrounds, dialogues,
    captions), puis estime, gate le budget et dispatche — tout en asynchrone.

    Suivre l'avancement via GET /api/jobs/{id} (statuts, shortfall, coûts)."""
    model = db.get(Model, payload.model_id)
    if model is None:
        raise HTTPException(404, "Model introuvable")
    if any(count < 0 for count in payload.counts_per_category.values()):
        raise HTTPException(422, "counts_per_category : valeurs négatives interdites")

    job = GenerationJob(
        model_id=model.id,
        counts_per_category=payload.counts_per_category,
        resolution=payload.resolution,
        duration_s=payload.duration_s,
        bitrate=payload.bitrate,
        model_variant=payload.model_variant,
        music_url=payload.music_url,
        budget_cap_usd=payload.budget_cap_usd,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    compose_job.delay(str(job.id))
    return job


@router.post("/estimate", response_model=schemas.EstimateResponse)
def estimate(payload: schemas.EstimateRequest, db: Session = Depends(get_db)):
    """Estimation live (fonction pure sur la table pricing, zéro appel API).
    Le taux QC par défaut est recalibré sur les derniers jobs (calibration_log)."""
    settings = get_settings()
    rates = load_rates(db)
    qc_rate = payload.qc_success_rate or get_calibrated_qc_rate(
        db, settings.default_qc_success_rate
    )
    spec = ItemSpec(
        count=payload.count,
        duration_s=payload.duration_s,
        resolution=payload.resolution,
        model=payload.model_variant,
        speaking=payload.speaking,
    )
    est = estimate_batch([spec], rates, qc_success_rate=qc_rate)
    max_videos = over = None
    if payload.budget_usd is not None:
        max_videos = max_videos_for_budget(payload.budget_usd, spec, rates, qc_rate)
        over = est.effective_usd > payload.budget_usd
    return schemas.EstimateResponse(
        gross_usd=est.gross_usd,
        effective_usd=est.effective_usd,
        qc_success_rate=est.qc_success_rate,
        cost_per_delivered_video_usd=round(est.cost_per_delivered_video_usd, 4),
        max_videos_for_budget=max_videos,
        over_budget=over,
    )


@router.post("/jobs", response_model=schemas.JobOut)
def create_job(payload: schemas.JobCreate, db: Session = Depends(get_db)):
    """Trigger manuel Phase 1 : une model, une catégorie, un prompt fourni.

    Compose les items (injection caractéristiques + refs), estime le coût,
    applique le budget cap, puis dispatche vers kie.ai via Celery.
    """
    settings = get_settings()
    model = db.get(Model, payload.model_id)
    if model is None:
        raise HTTPException(404, "Model introuvable")

    characteristics = [
        composer.CharacteristicInput(
            label=c.label,
            reference_image_url=c.reference_image_url,
            injection_hint=c.injection_hint,
            priority=c.priority,
            always_include=c.always_include,
        )
        for c in model.characteristics
    ]
    filled_prompt = composer.inject_characteristics(payload.prompt, characteristics)
    if payload.dialogue_script:
        filled_prompt = f"{filled_prompt}\n\nDialogue: {payload.dialogue_script}"
    refs = composer.select_reference_images(
        model.face_reference_url,
        characteristics,
        extra_refs=payload.extra_reference_urls,
        max_refs=settings.seedance_max_refs,
    )

    # Estimation + gate budget AVANT toute dépense
    rates = load_rates(db)
    spec = ItemSpec(
        count=payload.count,
        duration_s=payload.duration_s,
        resolution=payload.resolution,
        model=payload.model_variant,
        speaking=payload.dialogue_script is not None,
    )
    est = estimate_batch([spec], rates, qc_success_rate=settings.default_qc_success_rate)

    job = GenerationJob(
        model_id=model.id,
        counts_per_category={payload.category: payload.count},
        resolution=payload.resolution,
        duration_s=payload.duration_s,
        bitrate=payload.bitrate,
        model_variant=payload.model_variant,
        budget_cap_usd=payload.budget_cap_usd,
        estimated_cost_usd=est.gross_usd,
    )
    db.add(job)
    db.flush()

    per_item_cost = est.gross_usd / payload.count if payload.count else 0
    items = []
    for index in range(payload.count):
        items.append(
            JobItem(
                job_id=job.id,
                category=payload.category,
                characteristic_ids=[
                    str(c.id) for c in model.characteristics if c.always_include
                ],
                # Phase 1 : même prompt pour tout le batch → l'index désambiguïse.
                # Le moteur de variation de la Phase 2 hashera les vrais combos.
                combo_hash=composer.combo_hash(
                    {"prompt": filled_prompt, "refs": refs, "variant_index": index}
                ),
                filled_prompt=filled_prompt,
                dialogue_script=payload.dialogue_script,
                reference_image_urls=refs,
                item_estimated_cost=round(per_item_cost, 4),
            )
        )
    db.add_all(items)

    if payload.budget_cap_usd is not None and est.gross_usd > payload.budget_cap_usd:
        job.status = JobStatus.BLOCKED_BUDGET
        db.commit()
        db.refresh(job)
        return job

    db.commit()
    db.refresh(job)
    for item in items:
        dispatch_seedance.delay(str(item.id))
    return job


@router.get("/jobs/{job_id}", response_model=schemas.JobOut)
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db)):
    job = db.get(GenerationJob, job_id)
    if job is None:
        raise HTTPException(404, "Job introuvable")
    return job
