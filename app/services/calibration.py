"""Réconciliation estimé vs réel (brief §8, table §4.12).

Après chaque job terminé : log estimé / réel / taux de réussite QC.
L'estimateur relit ensuite un taux QC calibré sur les derniers batches —
la précision s'affine batch après batch.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CalibrationLog, GenerationJob, ItemStatus, QcStatus

CALIBRATION_WINDOW = 20  # nb de jobs récents utilisés pour le taux calibré
MIN_RATE = 0.05  # plancher : évite une division explosive sur un mauvais batch


def compute_qc_success_rate(items) -> float | None:
    """Taux de passage parmi les items réellement évalués par le QC."""
    evaluated = [i for i in items if i.qc_status in (QcStatus.PASS, QcStatus.FAIL)]
    if not evaluated:
        return None
    passed = sum(1 for i in evaluated if i.qc_status == QcStatus.PASS)
    return passed / len(evaluated)


def record_job_calibration(db: Session, job: GenerationJob) -> CalibrationLog:
    """À appeler quand tous les items du job sont terminaux."""
    actual = sum(
        (i.item_actual_cost if i.item_actual_cost is not None else (i.item_estimated_cost or 0))
        for i in job.items
        if i.status != ItemStatus.FAILED or i.qc_status == QcStatus.FAIL
    )
    job.actual_cost_usd = round(actual, 4)
    log = CalibrationLog(
        job_id=job.id,
        estimated_cost=job.estimated_cost_usd,
        actual_cost=job.actual_cost_usd,
        qc_success_rate=compute_qc_success_rate(job.items),
    )
    db.add(log)
    return log


def get_calibrated_qc_rate(db: Session, default: float) -> float:
    """Moyenne du taux QC sur les derniers jobs loggés ; default si aucun."""
    rates = db.scalars(
        select(CalibrationLog.qc_success_rate)
        .where(CalibrationLog.qc_success_rate.is_not(None))
        .order_by(CalibrationLog.created_at.desc())
        .limit(CALIBRATION_WINDOW)
    ).all()
    if not rates:
        return default
    return max(sum(rates) / len(rates), MIN_RATE)
