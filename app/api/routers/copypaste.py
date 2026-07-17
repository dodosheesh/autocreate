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
from app.media.probe import probe_video_duration
from app.services import composer, copypaste
from app.services.copypaste import MAX_REF_VIDEO_S
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
        tenant_id=user.tenant_id, video_url=video_url, label=label, weight=weight,
        duration_s=probe_video_duration(video_url),
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

    # Garde résolution AVANT dépense : Seedance FAST n'a pas de 1080p — kie.ai
    # renvoie sinon une 422 « Invalid resolution » opaque sur chaque item.
    if payload.resolution == "1080p" and "fast" in get_settings().kie_seedance_model:
        raise HTTPException(
            422,
            "1080p indisponible sur Seedance Fast (KIE_SEEDANCE_MODEL="
            f"{get_settings().kie_seedance_model}) : choisis 480p ou 720p, ou "
            "configure le modèle Standard (bytedance/seedance-2) qui supporte le 1080p.",
        )

    # La vidéo uploadée pour ce job rejoint la banque (dédup par URL) — sauf si
    # save_to_bank est décoché (test d'une vidéo sans polluer la banque).
    if payload.reference_video_url and payload.save_to_bank:
        _add_to_bank(db, user, payload.reference_video_url)

    if payload.use_bank:
        rows = db.scalars(tenant_query(ReferenceVideo, user)).all()
        # Seedance limite la vidéo de référence à 15 s : les trop longues sont
        # exclues du tirage (durée inconnue = laissée passer, kie tranchera).
        usable = [
            v for v in rows
            if v.duration_s is None or v.duration_s <= MAX_REF_VIDEO_S + 0.1
        ]
        if not usable:
            raise HTTPException(
                409,
                "Banque vidéo vide : uploade au moins une vidéo de référence"
                if not rows
                else f"Toutes les vidéos de la banque dépassent {MAX_REF_VIDEO_S:.0f} s "
                     "(limite Seedance) — coupe-les puis re-uploade.",
            )
        bank = [Option(id=str(v.id), weight=v.weight, text=v.video_url) for v in usable]
        videos = copypaste.pick_bank_videos(bank, payload.count, random.Random())
    elif payload.reference_video_url:
        # Durée : valeur sondée en banque si dispo, sinon probe direct (vidéo
        # non sauvegardée). > 15 s → refus clair AVANT d'envoyer à kie.ai.
        row = db.scalar(
            tenant_query(ReferenceVideo, user).where(
                ReferenceVideo.video_url == payload.reference_video_url
            )
        )
        duration = (
            row.duration_s
            if row is not None and row.duration_s is not None
            else probe_video_duration(payload.reference_video_url)
        )
        if duration is not None and duration > MAX_REF_VIDEO_S + 0.1:
            raise HTTPException(
                422,
                f"La vidéo de référence fait {duration:.1f} s — Seedance limite à "
                f"{MAX_REF_VIDEO_S:.0f} s. Coupe-la avant de relancer.",
            )
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
