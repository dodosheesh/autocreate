"""Chaîne Celery de la feature Pictures (nano banana).

    reverse_engineer_prompt(prompt_id)   (upload → vision → prompt sauvegardé)

    compose_picture_job(job) ──► estimate_and_gate_pictures(job)
                                      │ (budget cap)
                                      ▼
                                dispatch_nano_banana(item) ──► kie.ai
                                                                  │ webhook
                                                                  ▼
                                          process_picture_generated(item) :
                                          download → QC face-match (image directe)
                                          → scrub métadonnées → upload R2 → done
"""

import tempfile
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.db.base import db_session
from app.db.models import (
    Background,
    ItemStatus,
    JobStatus,
    Model,
    Outfit,
    PictureItem,
    PictureJob,
    PicturePrompt,
    PromptStatus,
    PromptTemplate,
    QcStatus,
)
from app.integrations import kie, r2
from app.integrations.vision import describe_image as _vision_describe
from app.integrations.vision import reverse_engineer_prompt as _vision_reverse
from app.integrations.vision import reverse_engineer_video_prompt as _vision_reverse_video
from app.media.frames import extract_keyframes
from app.media.scrub import strip_metadata
from app.services import picture_composer
from app.services.photo_styles import build_style_suffix
from app.services.composer import CharacteristicInput
from app.services.estimator import estimate_pictures
from app.services.pricing import load_rates
from app.services.template_library import ensure_slots
from app.services.variation import Option, outfit_option
from app.workers.celery_app import celery_app
from app.workers.tasks import _download, _pk


@celery_app.task(bind=True, max_retries=2)
def reverse_engineer_prompt(self, prompt_id: str) -> None:
    """Transforme l'image de référence uploadée en prompt réutilisable."""
    try:
        with db_session() as db:
            row = db.get(PicturePrompt, _pk(prompt_id))
            if row is None or row.status != PromptStatus.PENDING:
                return
            source_url = row.source_image_url
            model_desc = None  # description model optionnelle (branché plus tard)
        text = _vision_reverse(source_url, model_desc)
        with db_session() as db:
            row = db.get(PicturePrompt, _pk(prompt_id))
            if row is None or row.status != PromptStatus.PENDING:
                return  # supprimé ou déjà traité pendant l'appel vision
            row.prompt_text = text
            row.status = PromptStatus.READY
    except Exception as exc:
        # Re-tenter tant qu'il reste des essais (statut laissé à "pending"),
        # ne marquer "failed" qu'une fois épuisé — sinon le garde d'entrée
        # rendrait chaque ré-essai no-op.
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        with db_session() as db:
            row = db.get(PicturePrompt, _pk(prompt_id))
            if row is not None and row.status == PromptStatus.PENDING:
                row.status = PromptStatus.FAILED
                row.error = str(exc)[:2000]


_ASSET_MODELS = {"outfit": Outfit, "background": Background}


@celery_app.task(bind=True, max_retries=2)
def describe_asset(self, kind: str, asset_id: str, suffix: str = "") -> None:
    """Auto-description vision d'un outfit/background importé, + suffixe fourni
    par l'utilisateur. Stocke la phrase dans `tags` et passe le statut à ready."""
    db_model = _ASSET_MODELS.get(kind)
    if db_model is None:
        return
    try:
        with db_session() as db:
            row = db.get(db_model, _pk(asset_id))
            if row is None or row.status != "pending":
                return
            image_url = row.image_url
        desc = _vision_describe(image_url, kind)
        if suffix:
            desc = f"{desc} {suffix.strip()}"
        with db_session() as db:
            row = db.get(db_model, _pk(asset_id))
            if row is None or row.status != "pending":
                return  # supprimé ou déjà traité pendant l'appel vision
            row.tags = [desc]
            row.status = "ready"
    except Exception as exc:
        # On re-tente TANT QU'il reste des essais, en laissant le statut à
        # "pending" : sinon le garde d'entrée (status != pending) transformerait
        # chaque ré-essai en no-op. On ne marque "failed" qu'une fois épuisé.
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        with db_session() as db:
            row = db.get(db_model, _pk(asset_id))
            if row is not None and row.status == "pending":
                row.status = "failed"


@celery_app.task(bind=True, max_retries=2)
def reverse_engineer_video(self, template_id: str) -> None:
    """Vidéo de référence → template vidéo réutilisable (avec slots), stocké
    comme PromptTemplate. download → keyframes FFmpeg → vision → ensure_slots."""
    try:
        with db_session() as db:
            tmpl = db.get(PromptTemplate, _pk(template_id))
            if tmpl is None or tmpl.status != "pending":
                return
            source_url = tmpl.source_video_url
            speaking = tmpl.speaking

        with tempfile.TemporaryDirectory() as tmp:
            video_path = Path(tmp) / "ref.mp4"
            _download(source_url, video_path)
            frames = extract_keyframes(str(video_path), tmp, n=6)
            raw = _vision_reverse_video(frames, model_description=None, speaking=speaking)
        template_text = ensure_slots(raw, speaking)

        with db_session() as db:
            tmpl = db.get(PromptTemplate, _pk(template_id))
            if tmpl is None or tmpl.status != "pending":
                return  # supprimé ou déjà traité pendant le traitement
            tmpl.template_text = template_text
            tmpl.status = "ready"
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        with db_session() as db:
            tmpl = db.get(PromptTemplate, _pk(template_id))
            if tmpl is not None and tmpl.status == "pending":
                tmpl.status = "failed"
                tmpl.error = str(exc)[:2000]


def _picture_pools(db, tenant_id: str) -> tuple[list[Option], list[Option]]:
    prompts = [
        Option(id=str(p.id), weight=p.weight, text=p.prompt_text or "")
        for p in db.scalars(
            select(PicturePrompt).where(
                PicturePrompt.tenant_id == tenant_id,
                PicturePrompt.status == PromptStatus.READY,
            )
        ).all()
    ]
    outfits = [
        outfit_option(str(o.id), o.tags, o.image_url, o.weight)
        for o in db.scalars(
            select(Outfit).where(Outfit.tenant_id == tenant_id, Outfit.status == "ready")
        ).all()
    ]
    return prompts, outfits


@celery_app.task
def compose_picture_job(job_id: str) -> None:
    try:
        with db_session() as db:
            job = db.get(PictureJob, _pk(job_id))
            if job is None or job.status != JobStatus.PENDING:
                return
            job.status = JobStatus.COMPOSING
            model = db.get(Model, job.model_id)
            characteristics = [
                CharacteristicInput(
                    id=str(c.id),
                    label=c.label,
                    reference_image_url=c.reference_image_url,
                    injection_hint=c.injection_hint,
                    priority=c.priority,
                    recurring=c.recurring,
                    seedream=c.seedream,
                )
                for c in model.characteristics
            ]
            prompts, outfits = _picture_pools(db, job.tenant_id)

            result = picture_composer.compose_pictures(
                count=job.count,
                prompts=prompts,
                outfits=outfits,
                characteristics=characteristics,
                face_reference_url=model.face_reference_url,
                max_refs=get_settings().nano_banana_max_refs,
                style_suffix=build_style_suffix(job.styles),
            )
            for composed in result.items:
                db.add(
                    PictureItem(
                        job_id=job.id,
                        prompt_id=_pk(composed.prompt_id),
                        outfit_id=_pk(composed.outfit_id) if composed.outfit_id else None,
                        characteristic_ids=composed.characteristic_ids,
                        combo_hash=composed.combo_hash,
                        filled_prompt=composed.filled_prompt,
                        reference_image_urls=composed.reference_image_urls,
                    )
                )
            job.compose_shortfall = result.shortfall
        estimate_and_gate_pictures.delay(job_id)
    except picture_composer.PictureComposeError as exc:
        _fail_picture_job(job_id, f"compose: {exc}")
    except Exception as exc:
        _fail_picture_job(job_id, f"compose: {exc}")
        raise


@celery_app.task
def estimate_and_gate_pictures(job_id: str) -> None:
    try:
        with db_session() as db:
            job = db.get(PictureJob, _pk(job_id))
            if job is None or job.status != JobStatus.COMPOSING:
                return
            rates = load_rates(db)
            items = db.scalars(
                select(PictureItem).where(
                    PictureItem.job_id == job.id, PictureItem.status == ItemStatus.COMPOSED
                )
            ).all()
            est = estimate_pictures(len(items), rates, model=job.model_variant,
                                    resolution=job.image_resolution)
            unit = est.gross_usd / len(items) if items else 0
            for item in items:
                item.item_estimated_cost = round(unit, 4)
            job.estimated_cost_usd = est.gross_usd
            if job.budget_cap_usd is not None and est.gross_usd > job.budget_cap_usd:
                job.status = JobStatus.BLOCKED_BUDGET
                return
            job.status = JobStatus.DISPATCHED
            item_ids = [str(i.id) for i in items]
        for item_id in item_ids:
            dispatch_nano_banana.delay(item_id)
    except Exception as exc:
        _fail_picture_job(job_id, f"estimate_and_gate: {exc}")
        raise


@celery_app.task(bind=True, max_retries=3)
def dispatch_nano_banana(self, item_id: str) -> None:
    try:
        with db_session() as db:
            item = db.get(PictureItem, _pk(item_id))
            if item is None or item.status != ItemStatus.COMPOSED:
                return
            job = item.job
            payload = kie.build_seedream_input(
                prompt=item.filled_prompt,
                reference_image_urls=item.reference_image_urls,
                image_size=job.image_size,
                resolution=job.image_resolution,  # 1K/2K choisi par job
                output_format=job.output_format,
            )
            task_id = kie.create_seedream_task(payload)
            item.kie_task_id = task_id
            item.status = ItemStatus.DISPATCHED
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            _fail_picture_item(item_id, f"dispatch: {exc}")
            raise
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3)
def process_picture_generated(self, item_id: str) -> None:
    """download → QC face-match (image directe) → scrub métadonnées → R2."""
    settings = get_settings()
    try:
        with db_session() as db:
            item = db.get(PictureItem, _pk(item_id))
            if item is None or item.status != ItemStatus.GENERATED:
                return
            job = item.job
            model = db.get(Model, job.model_id)
            raw_url = item.raw_image_url
            face_url = model.face_reference_url
            output_format = job.output_format
            job_id = str(job.id)

        with tempfile.TemporaryDirectory() as tmp:
            raw_path = Path(tmp) / f"raw.{output_format}"
            _download(raw_url, raw_path)

            # QC : embedding directement sur l'image générée (pas d'extraction de frame)
            if settings.qc_enabled:
                from app.services import qc

                face_ref = Path(tmp) / "face_ref.jpg"
                _download(face_url, face_ref)
                score = qc.face_match_score(str(raw_path), str(face_ref))
                passed = score >= settings.qc_threshold
                with db_session() as db:
                    item = db.get(PictureItem, _pk(item_id))
                    item.face_match_score = round(score, 4)
                    item.qc_status = QcStatus.PASS if passed else QcStatus.FAIL
                    if not passed:
                        item.status = ItemStatus.FAILED
                        item.error = f"qc: face_match={score:.3f} < {settings.qc_threshold}"
                        _finalize_picture_job_if_done(db, item.job)
                        return
            else:
                with db_session() as db:
                    db.get(PictureItem, _pk(item_id)).qc_status = QcStatus.SKIPPED

            # Scrub complet des métadonnées avant mise à disposition (EXIF/XMP/
            # IPTC/ICC/C2PA). SynthID pixel n'est PAS retiré (cf. media/scrub).
            clean_path = Path(tmp) / f"clean.{output_format}"
            strip_metadata(str(raw_path), str(clean_path), output_format=output_format)
            content_type = "image/png" if output_format == "png" else "image/jpeg"
            final_url = r2.upload_file(
                str(clean_path),
                f"pictures/{job_id}/{item_id}.{output_format}",
                content_type=content_type,
            )

        with db_session() as db:
            item = db.get(PictureItem, _pk(item_id))
            item.final_image_url = final_url
            item.status = ItemStatus.DONE
            _finalize_picture_job_if_done(db, item.job)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            _fail_picture_item(item_id, f"process: {exc}")
            with db_session() as db:
                item = db.get(PictureItem, _pk(item_id))
                if item:
                    _finalize_picture_job_if_done(db, item.job)
            raise
        raise self.retry(exc=exc)


# ---------- helpers état ----------


def _fail_picture_item(item_id: str, error: str) -> None:
    with db_session() as db:
        item = db.get(PictureItem, _pk(item_id))
        if item:
            item.status = ItemStatus.FAILED
            item.error = error[:4000]


def _fail_picture_job(job_id: str, error: str) -> None:
    with db_session() as db:
        job = db.get(PictureJob, _pk(job_id))
        if job:
            job.status = JobStatus.FAILED
            job.error = error[:4000]


def _finalize_picture_job_if_done(db, job) -> None:
    statuses = {i.status for i in job.items}
    if not statuses or not statuses <= {ItemStatus.DONE, ItemStatus.FAILED}:
        return
    job.status = JobStatus.COMPLETED if ItemStatus.DONE in statuses else JobStatus.FAILED
    actual = sum(
        (i.item_actual_cost if i.item_actual_cost is not None else (i.item_estimated_cost or 0))
        for i in job.items
        if i.status != ItemStatus.FAILED or i.qc_status == QcStatus.FAIL
    )
    job.actual_cost_usd = round(actual, 4)


def _recheck_picture_items(job_pk) -> int:
    """Interroge kie.ai (recordInfo) pour les items image DISPATCHED et applique
    le résultat. job_pk=None → tous les tenants (beat) ; sinon un seul job."""
    with db_session() as db:
        q = select(PictureItem).where(PictureItem.status == ItemStatus.DISPATCHED)
        if job_pk is not None:
            q = q.where(PictureItem.job_id == job_pk)
        stale = [(str(i.id), i.kie_task_id) for i in db.scalars(q).all() if i.kie_task_id]
    for item_id, task_id in stale:
        try:
            apply_kie_picture_result(item_id, kie.get_task(task_id))
        except Exception:
            pass  # un item qui ne répond pas ne bloque pas les autres
    return len(stale)


def recheck_picture_job(job_id: str) -> int:
    """Pull à la demande des résultats kie.ai pour un job image (sans webhook)."""
    return _recheck_picture_items(_pk(job_id))


@celery_app.task
def poll_pending_picture_items() -> None:
    """Filet de sécurité (beat) : rattrape un callback perdu sur les items image."""
    _recheck_picture_items(None)


def apply_kie_picture_result(item_id: str, result: kie.KieTaskResult) -> None:
    """Applique un résultat kie.ai (webhook/polling) à un item image."""
    if result.state == "success" and result.result_urls:
        with db_session() as db:
            item = db.get(PictureItem, _pk(item_id))
            if item is None or item.status != ItemStatus.DISPATCHED:
                return
            item.raw_image_url = result.result_urls[0]
            if result.cost_usd is not None:
                item.item_actual_cost = result.cost_usd
            item.status = ItemStatus.GENERATED
        process_picture_generated.delay(item_id)
    elif result.state == "fail":
        retry = False
        with db_session() as db:
            item = db.get(PictureItem, _pk(item_id))
            if item is None or item.status != ItemStatus.DISPATCHED:
                return  # idempotent : webhook + poll ne doivent pas se cumuler
            msg = result.fail_msg or "génération échouée"
            if result.cost_usd is not None:
                item.item_actual_cost = result.cost_usd
            # Erreur transitoire kie.ai (surcharge) → on re-tente automatiquement.
            if kie.is_transient_failure(msg) and item.generation_attempts < get_settings().generation_max_retries:
                item.generation_attempts += 1
                item.status = ItemStatus.COMPOSED  # ré-éligible au dispatch
                item.error = f"kie.ai (re-essai {item.generation_attempts}): {msg}"
                retry = True
            else:
                item.status = ItemStatus.FAILED
                item.error = f"kie.ai: {msg}"
                _finalize_picture_job_if_done(db, item.job)
        if retry:  # léger délai pour laisser kie.ai récupérer
            dispatch_nano_banana.apply_async((item_id,), countdown=15)
