"""Chaîne Celery — Phase 1.

    dispatch_seedance(item) ──► kie.ai ──► webhook FastAPI
                                              │
                                              ▼
                                   process_generated(item)
                                   (download → assemble → R2 → done)

Phase 3 insérera qc_check et swap_voice entre generated et assembled ;
en attendant qc_status reste `skipped`.
"""

import tempfile
from pathlib import Path

import httpx

from app.db.base import db_session
from app.db.models import ItemStatus, JobItem, JobStatus, QcStatus
from app.integrations import kie, r2
from app.media.assemble import AssembleParams, assemble
from app.workers.celery_app import celery_app


def _download(url: str, dest: Path) -> None:
    with httpx.stream("GET", url, timeout=300, follow_redirects=True) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_bytes(1024 * 1024):
                f.write(chunk)


def _fail_item(item_id: str, error: str) -> None:
    with db_session() as db:
        item = db.get(JobItem, item_id)
        if item:
            item.status = ItemStatus.FAILED
            item.error = error[:4000]


@celery_app.task(bind=True, max_retries=3)
def dispatch_seedance(self, item_id: str) -> None:
    """Envoie l'item en génération chez kie.ai."""
    try:
        with db_session() as db:
            item = db.get(JobItem, item_id)
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
            item = db.get(JobItem, item_id)
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
            item = db.get(JobItem, item_id)
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
    from sqlalchemy import select

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
            item = db.get(JobItem, item_id)
            if item is None or item.status != ItemStatus.DISPATCHED:
                return
            item.raw_video_url = result.result_urls[0]
            item.status = ItemStatus.GENERATED
        process_generated.delay(item_id)
    elif result.state == "fail":
        _fail_item(item_id, f"kie.ai: {result.fail_msg or 'génération échouée'}")
