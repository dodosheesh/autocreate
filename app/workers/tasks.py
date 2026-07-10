"""Chaîne Celery — Phases 1-2.

    compose_job(job) ──► estimate_and_gate(job) ──► dispatch_seedance(item)
                              │ (budget cap)              │
                              ▼                           ▼
                        blocked_budget           kie.ai ──► webhook FastAPI
                                                              │
                                                              ▼
                                                   process_generated(item)
                                                   (download → assemble → R2)

Phase 3 insérera qc_check et swap_voice entre generated et assembled ;
en attendant qc_status reste `skipped`.
"""

import tempfile
import uuid
from pathlib import Path

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.db.base import db_session
from app.db.models import (
    Background,
    Caption,
    DialogueLine,
    GenerationJob,
    ItemStatus,
    JobItem,
    JobStatus,
    Model,
    Outfit,
    PromptTemplate,
    QcStatus,
)
from app.integrations import kie, r2
from app.media.assemble import AssembleParams, assemble
from app.services import variation
from app.services.composer import CharacteristicInput
from app.services.estimator import ItemSpec, estimate_item
from app.services.pricing import load_rates
from app.workers.celery_app import celery_app


def _pk(value: str | uuid.UUID) -> uuid.UUID:
    """Les IDs passent en str à travers le broker Celery ; les colonnes Uuid
    exigent des objets uuid.UUID."""
    return value if isinstance(value, uuid.UUID) else uuid.UUID(value)


def _download(url: str, dest: Path) -> None:
    with httpx.stream("GET", url, timeout=300, follow_redirects=True) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_bytes(1024 * 1024):
                f.write(chunk)


def _fail_item(item_id: str, error: str) -> None:
    with db_session() as db:
        item = db.get(JobItem, _pk(item_id))
        if item:
            item.status = ItemStatus.FAILED
            item.error = error[:4000]


def _fail_job(job_id: str, error: str) -> None:
    with db_session() as db:
        job = db.get(GenerationJob, _pk(job_id))
        if job:
            job.status = JobStatus.FAILED
            job.error = error[:4000]


def _build_pools(db, categories: list[str]) -> dict[str, variation.CategoryPools]:
    """Charge les banques Postgres en pools purs pour le moteur de variation.
    Outfits/backgrounds sont partagés entre catégories ; templates, dialogues
    et captions sont filtrés par catégorie."""
    outfits = [
        variation.outfit_option(str(o.id), o.tags, o.image_url, o.weight)
        for o in db.scalars(select(Outfit)).all()
    ]
    backgrounds = [
        variation.background_option(str(b.id), b.tags, b.image_url, b.weight)
        for b in db.scalars(select(Background)).all()
    ]
    pools: dict[str, variation.CategoryPools] = {}
    for category in categories:
        templates = [
            variation.TemplateOption(
                id=str(t.id),
                template_text=t.template_text,
                speaking=t.speaking,
                weight=t.weight,
            )
            for t in db.scalars(
                select(PromptTemplate).where(PromptTemplate.category == category)
            ).all()
        ]
        dialogues = [
            variation.Option(id=str(d.id), weight=d.weight, text=d.raw_text)
            for d in db.scalars(
                select(DialogueLine).where(DialogueLine.category == category)
            ).all()
        ]
        captions = [
            variation.Option(id=str(c.id), weight=c.weight, text=c.text)
            for c in db.scalars(select(Caption).where(Caption.category == category)).all()
        ]
        pools[category] = variation.CategoryPools(
            templates=templates,
            outfits=outfits,
            backgrounds=backgrounds,
            dialogues=dialogues,
            captions=captions,
        )
    return pools


@celery_app.task
def compose_job(job_id: str) -> None:
    """Compose les items d'un job batch depuis les banques (brief §5),
    puis enchaîne sur le gate budget."""
    try:
        with db_session() as db:
            job = db.get(GenerationJob, _pk(job_id))
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
            counts = {k: int(v) for k, v in (job.counts_per_category or {}).items()}
            pools = _build_pools(db, list(counts))

            result = variation.compose_batch(
                counts_per_category=counts,
                pools_by_category=pools,
                characteristics=characteristics,
                face_reference_url=model.face_reference_url,
                max_refs=get_settings().seedance_max_refs,
            )
            for composed in result.items:
                db.add(
                    JobItem(
                        job_id=job.id,
                        category=composed.category,
                        template_id=_pk(composed.template_id),
                        outfit_id=_pk(composed.outfit_id) if composed.outfit_id else None,
                        background_id=(
                            _pk(composed.background_id) if composed.background_id else None
                        ),
                        dialogue_line_id=(
                            _pk(composed.dialogue_id) if composed.dialogue_id else None
                        ),
                        caption_id=_pk(composed.caption_id) if composed.caption_id else None,
                        caption_text=composed.caption_text,
                        characteristic_ids=charac_ids,
                        combo_hash=composed.combo_hash,
                        filled_prompt=composed.filled_prompt,
                        dialogue_script=composed.dialogue_script,
                        reference_image_urls=composed.reference_image_urls,
                    )
                )
            job.compose_shortfall = result.shortfall_per_category
        estimate_and_gate.delay(job_id)
    except variation.ComposeError as exc:
        _fail_job(job_id, f"compose: {exc}")
    except Exception as exc:
        _fail_job(job_id, f"compose: {exc}")
        raise


@celery_app.task
def estimate_and_gate(job_id: str) -> None:
    """Estime le coût item par item (table pricing), bloque si budget_cap
    dépassé — AVANT toute dépense — sinon dispatche vers kie.ai."""
    try:
        settings = get_settings()
        with db_session() as db:
            job = db.get(GenerationJob, _pk(job_id))
            if job is None or job.status != JobStatus.COMPOSING:
                return
            rates = load_rates(db)
            items = db.scalars(
                select(JobItem).where(
                    JobItem.job_id == job.id, JobItem.status == ItemStatus.COMPOSED
                )
            ).all()
            gross = 0.0
            for item in items:
                cost = estimate_item(
                    ItemSpec(
                        count=1,
                        duration_s=job.duration_s,
                        resolution=job.resolution,
                        model=job.model_variant,
                        speaking=item.dialogue_script is not None,
                    ),
                    rates,
                )
                item.item_estimated_cost = round(cost, 4)
                gross += cost
            job.estimated_cost_usd = round(gross, 4)
            if job.budget_cap_usd is not None and gross > job.budget_cap_usd:
                job.status = JobStatus.BLOCKED_BUDGET
                return
            job.status = JobStatus.DISPATCHED
            item_ids = [str(i.id) for i in items]
        for item_id in item_ids:
            dispatch_seedance.delay(item_id)
    except Exception as exc:
        _fail_job(job_id, f"estimate_and_gate: {exc}")
        raise


@celery_app.task(bind=True, max_retries=3)
def dispatch_seedance(self, item_id: str) -> None:
    """Envoie l'item en génération chez kie.ai."""
    try:
        with db_session() as db:
            item = db.get(JobItem, _pk(item_id))
            if item is None or item.status != ItemStatus.COMPOSED:
                return
            job = item.job
            payload = kie.build_seedance_input(
                prompt=item.filled_prompt,
                reference_image_urls=item.reference_image_urls,
                resolution=job.resolution,
                duration_s=job.duration_s,
                aspect_ratio=job.aspect,
            )
            task_id = kie.create_seedance_task(payload)
            item.seedance_task_id = task_id
            item.status = ItemStatus.DISPATCHED
            if job.status == JobStatus.PENDING:
                job.status = JobStatus.DISPATCHED
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            _fail_item(item_id, f"dispatch: {exc}")
            raise
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3)
def process_generated(self, item_id: str) -> None:
    """Après retour kie.ai : télécharge la vidéo brute, assemble, upload R2."""
    try:
        with db_session() as db:
            item = db.get(JobItem, _pk(item_id))
            if item is None or item.status != ItemStatus.GENERATED:
                return
            job = item.job
            raw_url = item.raw_video_url
            resolution, bitrate = job.resolution, job.bitrate
            job_id = str(job.id)

        with tempfile.TemporaryDirectory() as tmp:
            raw_path = Path(tmp) / "raw.mp4"
            final_path = Path(tmp) / "final.mp4"
            _download(raw_url, raw_path)
            # Phase 1 : pas de QC ni de voice-swap — passage direct à l'assemblage
            assemble(
                str(raw_path),
                str(final_path),
                AssembleParams(resolution=resolution, bitrate=bitrate),
            )
            final_url = r2.upload_file(str(final_path), f"outputs/{job_id}/{item_id}.mp4")

        with db_session() as db:
            item = db.get(JobItem, _pk(item_id))
            item.qc_status = QcStatus.SKIPPED
            item.final_video_url = final_url
            item.status = ItemStatus.DONE
            job = item.job
            statuses = {i.status for i in job.items}
            if statuses <= {ItemStatus.DONE, ItemStatus.FAILED}:
                job.status = (
                    JobStatus.COMPLETED if ItemStatus.DONE in statuses else JobStatus.FAILED
                )
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            _fail_item(item_id, f"process: {exc}")
            raise
        raise self.retry(exc=exc)


@celery_app.task
def poll_pending_items() -> None:
    """Filet de sécurité si un webhook se perd : interroge kie.ai pour les
    items dispatchés. À brancher sur celery beat (optionnel en Phase 1)."""
    with db_session() as db:
        items = db.scalars(
            select(JobItem).where(JobItem.status == ItemStatus.DISPATCHED)
        ).all()
        stale = [(str(i.id), i.seedance_task_id) for i in items if i.seedance_task_id]
    for item_id, task_id in stale:
        result = kie.get_task(task_id)
        apply_kie_result(item_id, result)


def apply_kie_result(item_id: str, result: kie.KieTaskResult) -> None:
    """Applique un résultat kie.ai (webhook ou polling) et enchaîne."""
    if result.state == "success" and result.result_urls:
        with db_session() as db:
            item = db.get(JobItem, _pk(item_id))
            if item is None or item.status != ItemStatus.DISPATCHED:
                return
            item.raw_video_url = result.result_urls[0]
            item.status = ItemStatus.GENERATED
        process_generated.delay(item_id)
    elif result.state == "fail":
        _fail_item(item_id, f"kie.ai: {result.fail_msg or 'génération échouée'}")
