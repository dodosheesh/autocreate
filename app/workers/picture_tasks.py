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
    ItemStatus,
    JobStatus,
    Model,
    Outfit,
    PictureItem,
    PictureJob,
    PicturePrompt,
    PromptStatus,
    QcStatus,
)
from app.integrations import kie, r2
from app.integrations.vision import reverse_engineer_prompt as _vision_reverse
from app.media.scrub import strip_metadata
from app.services import picture_composer
from app.services.composer import CharacteristicInput
from app.services.estimator import estimate_pictures
from app.services.pricing import load_rates
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
            row.prompt_text = text
            row.status = PromptStatus.READY
    except Exception as exc:
        with db_session() as db:
            row = db.get(PicturePrompt, _pk(prompt_id))
            if row:
                row.status = PromptStatus.FAILED
                row.error = str(exc)[:2000]
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)


def _picture_pools(db) -> tuple[list[Option], list[Option]]:
    prompts = [
        Option(id=str(p.id), weight=p.weight, text=p.prompt_text or "")
        for p in db.scalars(
            select(PicturePrompt).where(PicturePrompt.status == PromptStatus.READY)
        ).all()
    ]
    outfits = [
        outfit_option(str(o.id), o.tags, o.image_url, o.weight)
        for o in db.scalars(select(Outfit)).all()
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
                    label=c.label,
                    reference_image_url=c.reference_image_url,
                    injection_hint=c.injection_hint,
                    priority=c.priority,
                    always_include=c.always_include,
                )
                for c in model.characteristics
            ]
            charac_ids = [str(c.id) for c in model.characteristics if c.always_include]
            prompts, outfits = _picture_pools(db)

            result = picture_composer.compose_pictures(
                count=job.count,
                prompts=prompts,
                outfits=outfits,
                characteristics=characteristics,
                face_reference_url=model.face_reference_url,
                max_refs=get_settings().nano_banana_max_refs,
            )
            for composed in result.items:
                db.add(
                    PictureItem(
                        job_id=job.id,
                        prompt_id=_pk(composed.prompt_id),
                        outfit_id=_pk(composed.outfit_id) if composed.outfit_id else None,
                        characteristic_ids=charac_ids,
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
            est = estimate_pictures(len(items), rates, model=job.model_variant)
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
            payload = kie.build_nano_banana_input(
                prompt=item.filled_prompt,
                reference_image_urls=item.reference_image_urls,
                image_size=job.image_size,
                output_format=job.output_format,
            )
            task_id = kie.create_nano_banana_task(payload)
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
        with db_session() as db:
            item = db.get(PictureItem, _pk(item_id))
            if item is None:
                return
            item.status = ItemStatus.FAILED
            item.error = f"kie.ai: {result.fail_msg or 'génération échouée'}"
            if result.cost_usd is not None:
                item.item_actual_cost = result.cost_usd
            _finalize_picture_job_if_done(db, item.job)
