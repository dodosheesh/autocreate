"""Feature copypaste (vidéo → vidéo) — protégée par auth.

Flow : uploader une vidéo de référence (/api/uploads) → POST /jobs avec cette
vidéo (elle rejoint automatiquement la banque vidéo) OU use_bank=true pour
piocher au hasard dans la banque déjà constituée. Chaque item envoie à Seedance
la vidéo de référence + la photo visage de la model avec le prompt fixe
« Replace the girl in the video with the girl in the picture » (+ custom).

Les jobs créés sont des GenerationJob standard (catégorie `copypaste`) : suivi,
recheck, review et export passent par les endpoints /api/jobs existants.
"""

import random
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api import schemas
from app.api.deps import current_user
from app.api.scope import owned, tenant_query
from app.config import get_settings
from app.db.base import get_db
from app.db.models import (
    Category,
    GenerationJob,
    JobItem,
    JobStatus,
    Model,
    Outfit,
    ReferenceVideo,
    User,
)
from app.services import composer, copypaste
from app.services.estimator import ItemSpec, estimate_batch
from app.services.pricing import load_rates
from app.services.variation import Option, outfit_option, weighted_draw
from app.workers.tasks import dispatch_seedance

router = APIRouter(prefix="/api/copypaste", tags=["copypaste"])


# ---------- banque de vidéos de référence ----------


def _add_to_bank(db: Session, user: User, video_url: str, label: str = "",
                 weight: float = 1.0) -> ReferenceVideo:
    """Ajout idempotent : la même URL n'est jamais dupliquée dans la banque."""
    existing = db.scalar(
        tenant_query(ReferenceVideo, user).where(ReferenceVideo.video_url == video_url)
    )
    if existing is not None:
        return existing
    row = ReferenceVideo(
        tenant_id=user.tenant_id, video_url=video_url, label=label, weight=weight
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.post("/videos", response_model=schemas.ReferenceVideoOut)
def add_video(
    payload: schemas.ReferenceVideoCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return _add_to_bank(db, user, payload.video_url, payload.label, payload.weight)


@router.get("/videos", response_model=list[schemas.ReferenceVideoOut])
def list_videos(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return db.scalars(
        tenant_query(ReferenceVideo, user).order_by(ReferenceVideo.created_at.desc())
    ).all()


@router.delete("/videos/{video_id}")
def delete_video(
    video_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    # Les items déjà générés gardent leur URL (pas de FK) : suppression sans risque.
    row = owned(db, ReferenceVideo, video_id, user)
    db.delete(row)
    db.commit()
    return {"deleted": str(video_id)}


# ---------- jobs ----------


@router.post("/jobs", response_model=schemas.JobOut)
def create_job(
    payload: schemas.CopypasteJobCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """N vidéos « copypaste » : chaque item = 1 vidéo de référence (fournie, ou
    piochée au hasard dans la banque) + photo visage + prompt fixe. Estimation
    et gate budget AVANT toute dépense, puis dispatch Seedance."""
    model = owned(db, Model, payload.model_id, user)

    # La vidéo uploadée pour ce job rejoint la banque (dédup par URL) — sauf si
    # save_to_bank est décoché (test d'une vidéo sans polluer la banque).
    if payload.reference_video_url and payload.save_to_bank:
        _add_to_bank(db, user, payload.reference_video_url)

    if payload.use_bank:
        bank = [
            Option(id=str(v.id), weight=v.weight, text=v.video_url)
            for v in db.scalars(tenant_query(ReferenceVideo, user)).all()
        ]
        videos = copypaste.pick_bank_videos(bank, payload.count, random.Random())
        if not videos:
            raise HTTPException(
                409, "Banque vidéo vide : uploade au moins une vidéo de référence"
            )
    elif payload.reference_video_url:
        videos = [payload.reference_video_url] * payload.count
    else:
        raise HTTPException(
            422, "reference_video_url requis (ou coche use_bank pour piocher la banque)"
        )

    base_prompt = copypaste.build_copypaste_prompt(payload.custom_prompt)

    # Assets aléatoires (case précochée) : caractéristiques (récurrentes + 1 du
    # pool, comme le moteur vidéo) et outfit tiré de la banque — par vidéo.
    # JAMAIS de background : le décor reste celui de la vidéo de référence.
    characteristics: list[composer.CharacteristicInput] = []
    outfits: list[Option] = []
    if payload.add_random_assets:
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
        outfits = [
            outfit_option(str(o.id), o.tags, o.image_url, o.weight)
            for o in db.scalars(
                tenant_query(Outfit, user).where(Outfit.status == "ready")
            ).all()
        ]

    # Estimation + gate budget AVANT toute dépense (même flux que /api/jobs).
    rates = load_rates(db)
    spec = ItemSpec(
        count=payload.count,
        duration_s=payload.duration_s,
        resolution=payload.resolution,
        model=payload.model_variant,
        speaking=False,  # pas de voice-swap : l'audio vient de la génération
    )
    est = estimate_batch(
        [spec], rates, qc_success_rate=get_settings().default_qc_success_rate
    )

    job = GenerationJob(
        tenant_id=user.tenant_id,
        model_id=model.id,
        counts_per_category={Category.COPYPASTE: payload.count},
        resolution=payload.resolution,
        duration_s=payload.duration_s,
        bitrate=payload.bitrate,
        model_variant=payload.model_variant,
        custom_prompt=payload.custom_prompt or None,
        budget_cap_usd=payload.budget_cap_usd,
        estimated_cost_usd=est.gross_usd,
    )
    db.add(job)
    db.flush()

    per_item_cost = est.gross_usd / payload.count if payload.count else 0
    rng = random.Random()
    max_refs = get_settings().seedance_max_refs
    items = []
    for index, video in enumerate(videos):
        outfit = weighted_draw(outfits, rng) if outfits else None
        active = (
            composer.select_active_characteristics(characteristics, rng)
            if characteristics
            else []
        )
        prompt = base_prompt
        if outfit:  # outfit.text = « wearing … »
            prompt = f"{prompt} She is {outfit.text}."
        prompt = composer.inject_characteristics(prompt, active)
        refs = composer.select_reference_images(
            model.face_reference_url,
            active,
            extra_refs=[outfit.image_url] if outfit and outfit.image_url else [],
            max_refs=max_refs,
        )
        items.append(
            JobItem(
                job_id=job.id,
                category=Category.COPYPASTE,
                outfit_id=uuid.UUID(outfit.id) if outfit else None,
                characteristic_ids=sorted(c.id for c in active),
                combo_hash=composer.combo_hash(
                    {
                        "prompt": prompt,
                        "video": video,
                        "outfit": outfit.id if outfit else None,
                        "characteristics": sorted(c.id for c in active),
                        "variant_index": index,
                    }
                ),
                filled_prompt=prompt,
                reference_image_urls=refs,
                reference_video_url=video,
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
