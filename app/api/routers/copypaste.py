"""Feature copypaste (vidéo → vidéo) — protégée par auth.

Flow : uploader une vidéo de référence (/api/uploads) → POST /jobs avec cette
vidéo (elle rejoint automatiquement la banque vidéo) OU use_bank=true pour
piocher au hasard dans la banque déjà constituée. Chaque item envoie à Seedance
la vidéo de référence + la photo visage de la model avec le prompt fixe
« Replace the girl in the video for the girl in the picture » (+ custom).

Les jobs créés sont des GenerationJob standard (catégorie `copypaste`) : suivi,
recheck, review et export passent par les endpoints /api/jobs existants.
"""

import random
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import schemas
from app.api.deps import current_user
from app.api.scope import owned, tenant_query
from app.config import get_settings
from app.db.base import get_db
from app.db.models import (
    Category,
    GenerationJob,
    ItemStatus,
    JobItem,
    JobStatus,
    Model,
    Outfit,
    ReferenceVideo,
    User,
)
from app.media.probe import (
    SEEDANCE_MAX_FPS,
    SEEDANCE_MIN_FPS,
    downscale_reference_video,
    fps_out_of_range,
    normalize_reference_video,
    probe_video_info,
    strip_reference_video_audio,
)
from app.services import composer, copypaste
from app.services.copypaste import MAX_REF_VIDEO_S
from app.services.estimator import ItemSpec, estimate_batch
from app.services.pricing import load_rates
from app.services.variation import Option, outfit_option, weighted_draw
from app.workers.tasks import dispatch_seedance

router = APIRouter(prefix="/api/copypaste", tags=["copypaste"])


# ---------- banque de vidéos de référence ----------


def _owned_video(db: Session, video_id: uuid.UUID, user: User, model_id: uuid.UUID | None) -> ReferenceVideo:
    row = owned(db, ReferenceVideo, video_id, user)
    if model_id is not None:
        owned(db, Model, model_id, user)
        if row.model_id != model_id:
            raise HTTPException(404, "Vidéo introuvable pour cette model")
    return row


def _add_to_bank(db: Session, user: User, video_url: str, label: str = "",
                 weight: float = 1.0, theme: str = "", model_id=None) -> ReferenceVideo:
    """Ajout idempotent : la même URL n'est jamais dupliquée dans la banque.
    Un thème explicitement fourni re-range une vidéo déjà présente."""
    theme = (theme or "").strip()
    existing = db.scalar(
        tenant_query(ReferenceVideo, user).where(
            ReferenceVideo.video_url == video_url, ReferenceVideo.model_id == model_id
        )
    )
    if existing is not None:
        if theme and existing.theme != theme:
            existing.theme = theme
            db.commit()
            db.refresh(existing)
        return existing
    info = probe_video_info(video_url)
    duration_s, fps = info.duration_s, info.fps
    if fps_out_of_range(fps):
        # Seedance exige 23,8–60 fps → re-encodage auto à 30 fps. Si ça échoue,
        # la vidéo est gardée mais exclue des tirages (bouton 🔧 pour re-tenter).
        try:
            video_url, info = normalize_reference_video(video_url, user.tenant_id)
            duration_s, fps = info.duration_s, info.fps
        except Exception:
            pass
    row = ReferenceVideo(
        tenant_id=user.tenant_id, model_id=model_id, video_url=video_url, label=label, weight=weight,
        theme=theme, duration_s=duration_s, fps=fps,
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
    model = owned(db, Model, payload.model_id, user) if payload.model_id else None
    return _add_to_bank(
        db, user, payload.video_url, payload.label, payload.weight, payload.theme, model.id if model else None
    )


@router.patch("/videos/{video_id}", response_model=schemas.ReferenceVideoOut)
def update_video(
    video_id: uuid.UUID,
    payload: schemas.ReferenceVideoUpdate,
    model_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Rangement : change le thème (ou label/poids) d'une vidéo de la banque."""
    row = _owned_video(db, video_id, user, model_id)
    if payload.theme is not None:
        row.theme = payload.theme.strip()
    if payload.label is not None:
        row.label = payload.label
    if payload.weight is not None:
        row.weight = payload.weight
    db.commit()
    db.refresh(row)
    return row


@router.post("/videos/{video_id}/normalize", response_model=schemas.ReferenceVideoOut)
def normalize_video(
    video_id: uuid.UUID, model_id: uuid.UUID | None = None,
    db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Re-sonde une vidéo de la banque et la re-encode à 30 fps si son frame
    rate est hors plage Seedance (23,8–60). Sert aussi à réparer les vidéos
    ajoutées avant l'introduction de la sonde fps."""
    row = _owned_video(db, video_id, user, model_id)
    info = probe_video_info(row.video_url)
    row.duration_s, row.fps = info.duration_s, info.fps
    if fps_out_of_range(info.fps):
        try:
            new_url, new_info = normalize_reference_video(row.video_url, user.tenant_id)
        except Exception as exc:
            db.commit()  # garde au moins les infos sondées
            raise HTTPException(502, f"Normalisation échouée : {exc}")
        row.video_url = new_url
        row.duration_s, row.fps = new_info.duration_s, new_info.fps
    db.commit()
    db.refresh(row)
    return row


@router.get("/videos", response_model=list[schemas.ReferenceVideoOut])
def list_videos(
    model_id: uuid.UUID | None = None,
    db: Session = Depends(get_db), user: User = Depends(current_user),
):
    query = tenant_query(ReferenceVideo, user)
    if model_id is not None:
        owned(db, Model, model_id, user)
        query = query.where(ReferenceVideo.model_id == model_id)
    return db.scalars(query.order_by(ReferenceVideo.created_at.desc())).all()


@router.delete("/videos/{video_id}")
def delete_video(
    video_id: uuid.UUID, model_id: uuid.UUID | None = None,
    db: Session = Depends(get_db), user: User = Depends(current_user)
):
    # Les items déjà générés gardent leur URL (pas de FK) : suppression sans risque.
    row = _owned_video(db, video_id, user, model_id)
    db.delete(row)
    db.commit()
    return {"deleted": str(video_id)}


@router.post("/jobs/{job_id}/items/{item_id}/strip-audio-retry")
def strip_audio_and_retry(
    job_id: uuid.UUID,
    item_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Répare un refus de sûreté audio et relance les items concernés.

    L'action est volontairement disponible seulement pour un item Copypaste
    explicitement refusé par le filtre audio. La ligne de banque de la model
    est remplacée par sa copie silencieuse, sans toucher aux autres models.
    """
    job = owned(db, GenerationJob, job_id, user)
    # JobItem n'a pas de tenant_id propre : le job, déjà chargé via owned(),
    # porte l'isolation de tenant.
    item = db.scalar(select(JobItem).where(JobItem.id == item_id, JobItem.job_id == job.id))
    if item is None or item.category != Category.COPYPASTE or not item.reference_video_url:
        raise HTTPException(404, "Item Copypaste introuvable")
    if item.status != ItemStatus.FAILED or not copypaste.is_audio_safety_rejection(item.error):
        raise HTTPException(409, "Cette réparation est réservée aux refus explicites liés à l'audio")

    old_url = item.reference_video_url
    try:
        silent_url, info = strip_reference_video_audio(old_url, user.tenant_id)
    except Exception as exc:
        raise HTTPException(502, f"Suppression audio échouée : {exc}") from exc

    # Remplace l'entrée de banque (même thème, label et poids), au lieu de
    # la dupliquer. Ainsi l'ancienne vidéo sonore n'est plus proposée.
    bank_rows = db.scalars(
        tenant_query(ReferenceVideo, user).where(
            ReferenceVideo.model_id == job.model_id,
            ReferenceVideo.video_url == old_url,
        )
    ).all()
    for row in bank_rows:
        row.video_url = silent_url
        row.duration_s, row.fps = info.duration_s, info.fps

    # Une même référence peut avoir été utilisée plusieurs fois dans le
    # job. On relance exactement les échecs audio qui l'emploient, jamais les
    # autres échecs ni les items déjà terminés.
    retry_items = [
        candidate for candidate in job.items
        if candidate.category == Category.COPYPASTE
        and candidate.status == ItemStatus.FAILED
        and candidate.reference_video_url == old_url
        and copypaste.is_audio_safety_rejection(candidate.error)
    ]
    for candidate in retry_items:
        candidate.reference_video_url = silent_url
        candidate.seedance_task_id = None
        candidate.raw_video_url = None
        candidate.final_video_url = None
        candidate.error = None
        candidate.status = ItemStatus.COMPOSED
        candidate.generation_attempts = 0
    job.status = JobStatus.DISPATCHED
    job.error = None
    db.commit()

    for candidate in retry_items:
        dispatch_seedance.delay(str(candidate.id))
    return {"retried_items": len(retry_items), "video_url": silent_url}


@router.post("/jobs/{job_id}/items/{item_id}/downscale-retry")
def downscale_and_retry(
    job_id: uuid.UUID,
    item_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Réduit une référence trop grande à 1080p et relance les échecs liés."""
    job = owned(db, GenerationJob, job_id, user)
    item = db.scalar(select(JobItem).where(JobItem.id == item_id, JobItem.job_id == job.id))
    if item is None or item.category != Category.COPYPASTE or not item.reference_video_url:
        raise HTTPException(404, "Item Copypaste introuvable")
    if item.status != ItemStatus.FAILED or not copypaste.is_video_pixel_limit_rejection(item.error):
        raise HTTPException(409, "Cette réparation est réservée aux refus de limite de pixels vidéo")

    old_url = item.reference_video_url
    try:
        resized_url, info = downscale_reference_video(old_url, user.tenant_id)
    except Exception as exc:
        raise HTTPException(502, f"Réduction 1080p échouée : {exc}") from exc

    for row in db.scalars(
        tenant_query(ReferenceVideo, user).where(
            ReferenceVideo.model_id == job.model_id,
            ReferenceVideo.video_url == old_url,
        )
    ).all():
        row.video_url = resized_url
        row.duration_s, row.fps = info.duration_s, info.fps

    retry_items = [
        candidate for candidate in job.items
        if candidate.category == Category.COPYPASTE
        and candidate.status == ItemStatus.FAILED
        and candidate.reference_video_url == old_url
        and copypaste.is_video_pixel_limit_rejection(candidate.error)
    ]
    for candidate in retry_items:
        candidate.reference_video_url = resized_url
        candidate.seedance_task_id = None
        candidate.raw_video_url = None
        candidate.final_video_url = None
        candidate.error = None
        candidate.status = ItemStatus.COMPOSED
        candidate.generation_attempts = 0
    job.status = JobStatus.DISPATCHED
    job.error = None
    db.commit()

    for candidate in retry_items:
        dispatch_seedance.delay(str(candidate.id))
    return {"retried_items": len(retry_items), "video_url": resized_url}


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
    settings = get_settings()

    # Qualité par job : Standard (meilleure qualité, défaut de la feature) ou Fast.
    kie_model = (
        settings.kie_seedance_standard_model
        if payload.seedance_quality == "standard"
        else settings.kie_seedance_model
    )
    # Garde résolution AVANT dépense : Seedance FAST n'a pas de 1080p — kie.ai
    # renvoie sinon une 422 « Invalid resolution » opaque sur chaque item.
    if payload.resolution == "1080p" and "fast" in kie_model:
        raise HTTPException(
            422,
            f"1080p indisponible sur Seedance Fast ({kie_model}) : choisis 480p ou "
            "720p, ou passe la qualité du job sur Standard.",
        )

    # La vidéo uploadée pour ce job rejoint la banque (dédup par URL) — sauf si
    # save_to_bank est décoché (test d'une vidéo sans polluer la banque).
    # Thème appliqué : video_theme, sinon le thème (de pioche) resté sélectionné
    # — l'upload suit toujours le thème visible dans l'UI.
    saved_row = None
    if payload.reference_video_url and payload.save_to_bank:
        upload_theme = (payload.video_theme or "").strip() or (payload.theme or "").strip()
        saved_row = _add_to_bank(db, user, payload.reference_video_url, theme=upload_theme, model_id=model.id)

    if payload.reference_video_ids:
        # Sélection précise : la génération est répartie UNIQUEMENT sur ces
        # vidéos (round-robin sur un ordre mélangé → répartition équilibrée).
        rows = db.scalars(
            tenant_query(ReferenceVideo, user).where(
                ReferenceVideo.id.in_(payload.reference_video_ids), ReferenceVideo.model_id == model.id
            )
        ).all()
        if len(rows) != len(set(payload.reference_video_ids)):
            raise HTTPException(404, "Vidéo(s) sélectionnée(s) introuvable(s) dans la banque")
        too_long = [
            v for v in rows
            if v.duration_s is not None and v.duration_s > MAX_REF_VIDEO_S + 0.1
        ]
        if too_long:
            names = ", ".join(
                (v.label or v.video_url.rsplit("/", 1)[-1]) for v in too_long
            )
            raise HTTPException(
                422,
                f"Sélection invalide : {names} dépasse(nt) {MAX_REF_VIDEO_S:.0f} s "
                "(limite Seedance) — retire-la(les) de la sélection.",
            )
        bad_fps = [v for v in rows if fps_out_of_range(v.fps)]
        if bad_fps:
            names = ", ".join(
                (v.label or v.video_url.rsplit("/", 1)[-1]) for v in bad_fps
            )
            raise HTTPException(
                422,
                f"Sélection invalide : {names} a/ont un frame rate hors plage Seedance "
                f"({SEEDANCE_MIN_FPS}–{SEEDANCE_MAX_FPS:.0f} fps) — clique 🔧 dans la "
                "banque pour normaliser à 30 fps.",
            )
        # Sélection : `count` = générations PAR vidéo sélectionnée (chaque vidéo
        # cochée est TOUJOURS utilisée — sélection de 5 + count 1 → 5 vidéos).
        urls = [v.video_url for v in rows]
        random.Random().shuffle(urls)
        videos = [urls[i % len(urls)] for i in range(len(urls) * payload.count)]
        if len(videos) > 200:
            raise HTTPException(
                422,
                f"{len(urls)} vidéo(s) × {payload.count} génération(s) = {len(videos)} "
                "vidéos demandées (max 200) — baisse « Vidéos à générer » ou la sélection.",
            )
    elif payload.use_bank:
        rows = db.scalars(
            tenant_query(ReferenceVideo, user).where(ReferenceVideo.model_id == model.id)
        ).all()
        # Pioche restreinte à UN thème : les autres thèmes ne sont JAMAIS tirés.
        theme = (payload.theme or "").strip()
        if theme:
            rows = [v for v in rows if (v.theme or "").strip().lower() == theme.lower()]
            if not rows:
                raise HTTPException(
                    409,
                    f"Aucune vidéo dans le thème « {theme} » — range des vidéos "
                    "dans ce thème d'abord (bouton ✎ de la banque).",
                )
        # Seedance limite la référence à 15 s et 23,8–60 fps : les vidéos hors
        # contraintes sont exclues du tirage (inconnues = laissées passer).
        usable = [
            v for v in rows
            if (v.duration_s is None or v.duration_s <= MAX_REF_VIDEO_S + 0.1)
            and not fps_out_of_range(v.fps)
        ]
        if not usable:
            raise HTTPException(
                409,
                "Banque vidéo vide : uploade au moins une vidéo de référence"
                if not rows
                else f"Toutes les vidéos {'du thème « ' + theme + ' »' if theme else 'de la banque'} "
                     f"dépassent {MAX_REF_VIDEO_S:.0f} s (limite Seedance) — coupe-les puis re-uploade.",
            )
        bank = [Option(id=str(v.id), weight=v.weight, text=v.video_url) for v in usable]
        videos = copypaste.pick_bank_videos(bank, payload.count, random.Random())
    elif payload.reference_video_url:
        # Infos : ligne de banque si dispo (déjà sondée/normalisée), sinon probe
        # direct (vidéo non sauvegardée). Hors contraintes Seedance → refus ou
        # normalisation AVANT d'envoyer à kie.ai.
        row = saved_row or db.scalar(
            tenant_query(ReferenceVideo, user).where(
                ReferenceVideo.video_url == payload.reference_video_url,
                ReferenceVideo.model_id == model.id,
            )
        )
        if row is not None:
            direct_url, duration, fps = row.video_url, row.duration_s, row.fps
        else:
            info = probe_video_info(payload.reference_video_url)
            direct_url, duration, fps = payload.reference_video_url, info.duration_s, info.fps
        if duration is not None and duration > MAX_REF_VIDEO_S + 0.1:
            raise HTTPException(
                422,
                f"La vidéo de référence fait {duration:.1f} s — Seedance limite à "
                f"{MAX_REF_VIDEO_S:.0f} s. Coupe-la avant de relancer.",
            )
        if fps_out_of_range(fps):
            # Vidéo non banquée (ou normalisation échouée à l'ajout) → re-tente.
            try:
                direct_url, info = normalize_reference_video(direct_url, user.tenant_id)
                if row is not None:  # répare aussi la banque
                    row.video_url = direct_url
                    row.duration_s, row.fps = info.duration_s, info.fps
                    db.commit()
            except Exception as exc:
                raise HTTPException(
                    422,
                    f"Frame rate {fps:.1f} fps hors plage Seedance "
                    f"({SEEDANCE_MIN_FPS}–{SEEDANCE_MAX_FPS:.0f}) et la normalisation "
                    f"automatique a échoué : {exc}",
                )
        videos = [direct_url] * payload.count
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
                tenant_query(Outfit, user).where(Outfit.model_id == model.id, Outfit.status == "ready")
            ).all()
        ]

    # Estimation + gate budget AVANT toute dépense (même flux que /api/jobs).
    # total_count ≠ payload.count en mode sélection (count × vidéos cochées).
    total_count = len(videos)
    rates = load_rates(db)
    spec = ItemSpec(
        count=total_count,
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
        counts_per_category={Category.COPYPASTE: total_count},
        resolution=payload.resolution,
        duration_s=payload.duration_s,
        bitrate=payload.bitrate,
        model_variant=payload.model_variant,
        kie_model=kie_model,
        custom_prompt=payload.custom_prompt or None,
        budget_cap_usd=payload.budget_cap_usd,
        estimated_cost_usd=est.gross_usd,
    )
    db.add(job)
    db.flush()

    per_item_cost = est.gross_usd / total_count if total_count else 0
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
    downscale_reference_video,
