import random
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
    GenerationJob,
    ItemStatus,
    JobItem,
    JobStatus,
    Model,
    PromptTemplate,
    ReviewStatus,
    User,
)
from app.services import composer, variation
from app.services.calibration import get_calibrated_qc_rate
from app.services.estimator import ItemSpec, estimate_batch, max_videos_for_budget
from app.services.export_zip import build_media_zip
from app.services.pricing import load_rates
from app.workers.tasks import compose_job, dispatch_seedance, recheck_video_job

router = APIRouter(prefix="/api", tags=["jobs"])


@router.post("/jobs/batch", response_model=schemas.JobOut)
def create_batch_job(
    payload: schemas.BatchJobCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Job batch Phase 2 : le moteur compose N variantes dédupliquées par
    catégorie depuis les banques (templates, outfits, backgrounds, dialogues,
    captions), puis estime, gate le budget et dispatche — tout en asynchrone.

    Suivre l'avancement via GET /api/jobs/{id} (statuts, shortfall, coûts)."""
    owned(db, Model, payload.model_id, user)
    if any(count < 0 for count in payload.counts_per_category.values()):
        raise HTTPException(422, "counts_per_category : valeurs négatives interdites")
    # Seedance FAST n'a pas de 1080p — refus clair AVANT dépense plutôt qu'une
    # 422 « Invalid resolution » kie.ai sur chaque item.
    if payload.resolution == "1080p" and "fast" in get_settings().kie_seedance_model:
        raise HTTPException(
            422,
            "1080p indisponible sur Seedance Fast : choisis 480p ou 720p, ou configure "
            "KIE_SEEDANCE_MODEL sur le modèle Standard (bytedance/seedance-2).",
        )

    job = GenerationJob(
        tenant_id=user.tenant_id,
        model_id=payload.model_id,
        counts_per_category=payload.counts_per_category,
        resolution=payload.resolution,
        duration_s=payload.duration_s,
        bitrate=payload.bitrate,
        model_variant=payload.model_variant,
        music_url=payload.music_url,
        custom_prompt=payload.custom_prompt,
        omit_background=payload.omit_background,
        budget_cap_usd=payload.budget_cap_usd,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    compose_job.delay(str(job.id))
    return job


@router.post("/estimate", response_model=schemas.EstimateResponse)
def estimate(
    payload: schemas.EstimateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
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
def create_job(
    payload: schemas.JobCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Trigger manuel Phase 1 : une model, une catégorie, un prompt fourni.

    Compose les items (injection caractéristiques + refs), estime le coût,
    applique le budget cap, puis dispatche vers kie.ai via Celery.
    """
    settings = get_settings()
    model = owned(db, Model, payload.model_id, user)

    characteristics = [
        composer.CharacteristicInput(
            id=str(c.id),
            label=c.label,
            reference_image_url=c.reference_image_url,
            injection_hint=c.injection_hint,
            priority=c.priority,
            recurring=c.recurring,
        )
        for c in model.characteristics
    ]
    # Récurrentes (tatouage) + 1 caractéristique aléatoire (cohérent avec le moteur)
    active = composer.select_active_characteristics(characteristics, random.Random())
    active_ids = [c.id for c in active]
    filled_prompt = composer.inject_characteristics(payload.prompt, active)
    if payload.dialogue_script:
        filled_prompt = f"{filled_prompt}\n\nDialogue: {payload.dialogue_script}"
    refs = composer.select_reference_images(
        model.face_reference_url,
        active,
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
        tenant_id=user.tenant_id,
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
                characteristic_ids=active_ids,
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


@router.post("/estimate/batch", response_model=schemas.BatchEstimateResponse)
def estimate_batch_endpoint(
    payload: schemas.BatchEstimateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Estimation live du panneau de génération, multi-catégories.

    Une catégorie est considérée parlante (coût voice-swap inclus) si au
    moins un de ses templates est `speaking`. Le taux QC vient de la
    calibration (calibration_log) sauf s'il est fourni explicitement."""
    settings = get_settings()
    rates = load_rates(db)
    qc_rate = payload.qc_success_rate or get_calibrated_qc_rate(
        db, settings.default_qc_success_rate
    )
    speaking_categories = set(
        db.scalars(
            tenant_query(PromptTemplate, user)
            .where(PromptTemplate.speaking.is_(True))
            .with_only_columns(PromptTemplate.category)
            .distinct()
        ).all()
    )
    specs: list[tuple[str, ItemSpec]] = [
        (
            category,
            ItemSpec(
                count=count,
                # Format long 30 s = 2 clips de 15 s → facturé sur 30 s.
                duration_s=(
                    variation.LONG_FORM_TOTAL_S
                    if category == variation.LONG_FORM_CATEGORY
                    else payload.duration_s
                ),
                resolution=payload.resolution,
                model=payload.model_variant,
                speaking=(
                    category == variation.LONG_FORM_CATEGORY
                    or category in speaking_categories
                ),
            ),
        )
        for category, count in payload.counts_per_category.items()
        if count > 0
    ]
    est = estimate_batch([spec for _, spec in specs], rates, qc_success_rate=qc_rate)
    per_category = {
        category: round(
            estimate_batch([spec], rates, qc_success_rate=qc_rate).gross_usd, 4
        )
        for category, spec in specs
    }
    max_videos = over = None
    if payload.budget_usd is not None:
        over = est.effective_usd > payload.budget_usd
        if specs:
            max_videos = max_videos_for_budget(
                payload.budget_usd,
                ItemSpec(
                    count=1,
                    duration_s=payload.duration_s,
                    resolution=payload.resolution,
                    model=payload.model_variant,
                    speaking=bool(speaking_categories),
                ),
                rates,
                qc_rate,
            )
    return schemas.BatchEstimateResponse(
        gross_usd=est.gross_usd,
        effective_usd=est.effective_usd,
        qc_success_rate=round(qc_rate, 4),
        cost_per_delivered_video_usd=round(est.cost_per_delivered_video_usd, 4),
        max_videos_for_budget=max_videos,
        over_budget=over,
        total_items=est.total_items,
        per_category_usd=per_category,
    )


@router.get("/jobs", response_model=list[schemas.JobSummaryOut])
def list_jobs(
    limit: int = 50,
    kind: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """`kind=video` exclut les jobs copypaste, `kind=copypaste` ne garde qu'eux
    (filtré côté serveur : sinon les jobs récents d'un type poussent l'autre
    type hors de la fenêtre `limit` et sa liste paraît vide)."""
    rows = db.scalars(
        tenant_query(GenerationJob, user)
        .order_by(GenerationJob.created_at.desc())
        .limit(2000 if kind else limit)
    ).all()
    if kind == "copypaste":
        rows = [j for j in rows if (j.counts_per_category or {}).get("copypaste") is not None]
    elif kind == "video":
        rows = [j for j in rows if (j.counts_per_category or {}).get("copypaste") is None]
    return rows[:limit]


@router.get("/jobs/{job_id}", response_model=schemas.JobOut)
def get_job(
    job_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    return owned(db, GenerationJob, job_id, user)


@router.post("/jobs/{job_id}/recheck", response_model=schemas.JobOut)
def recheck_job(
    job_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Interroge kie.ai à la demande pour les items dispatchés (si un webhook
    s'est perdu). Ne dépend ni du webhook ni de celery beat."""
    job = owned(db, GenerationJob, job_id, user)
    recheck_video_job(str(job_id))
    db.refresh(job)
    return job


@router.post("/items/{item_id}/review", response_model=schemas.ItemOut)
def review_item(
    item_id: uuid.UUID,
    payload: schemas.ReviewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Grille de review : approuver / rejeter un item livré."""
    item = db.get(JobItem, item_id)
    if item is None or item.job.tenant_id != user.tenant_id:
        raise HTTPException(404, "Item introuvable")
    if item.status != ItemStatus.DONE:
        raise HTTPException(409, f"Item non livrable (statut {item.status})")
    item.review_status = payload.decision
    db.commit()
    db.refresh(item)
    return item


@router.post("/jobs/{job_id}/approve-all", response_model=schemas.JobOut)
def approve_all(
    job_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Approuve d'un coup toutes les vidéos livrées (statut DONE) du job."""
    job = owned(db, GenerationJob, job_id, user)
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
    """Export des items approuvés : URLs finales + contexte (caption, catégorie)."""
    job = owned(db, GenerationJob, job_id, user)
    approved = [
        item
        for item in job.items
        if item.review_status == ReviewStatus.APPROVED and item.final_video_url
    ]
    return schemas.ExportOut(
        job_id=job.id,
        approved_count=len(approved),
        videos=[
            {
                "item_id": str(item.id),
                "category": item.category,
                "url": item.final_video_url,
                "caption": item.caption_text,
            }
            for item in approved
        ],
    )


@router.get("/jobs/{job_id}/export.zip")
def export_job_zip(
    job_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """ZIP de toutes les vidéos approuvées du job (fichiers + manifest.json)."""
    job = owned(db, GenerationJob, job_id, user)
    approved = [
        i for i in job.items
        if i.review_status == ReviewStatus.APPROVED and i.final_video_url
    ]
    if not approved:
        raise HTTPException(409, "Aucune vidéo approuvée à exporter")
    entries = [
        {"item_id": i.id, "url": i.final_video_url, "prompt": i.filled_prompt}
        for i in approved
    ]
    data = build_media_zip(entries, default_ext="mp4")
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="videos_{str(job_id)[:8]}.zip"'},
    )
