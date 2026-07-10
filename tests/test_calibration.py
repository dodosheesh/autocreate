import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import CalibrationLog, GenerationJob, ItemStatus, JobItem, Model, QcStatus
from app.services.calibration import (
    compute_qc_success_rate,
    get_calibrated_qc_rate,
    record_job_calibration,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


class FakeItem:
    def __init__(self, qc_status, status=ItemStatus.DONE, actual=None, estimated=1.0):
        self.qc_status = qc_status
        self.status = status
        self.item_actual_cost = actual
        self.item_estimated_cost = estimated


def test_taux_qc_sur_items_evalues_uniquement():
    items = [
        FakeItem(QcStatus.PASS),
        FakeItem(QcStatus.PASS),
        FakeItem(QcStatus.FAIL),
        FakeItem(QcStatus.SKIPPED),  # non évalué → ignoré
    ]
    assert compute_qc_success_rate(items) == pytest.approx(2 / 3)


def test_taux_qc_none_si_rien_evalue():
    assert compute_qc_success_rate([FakeItem(QcStatus.SKIPPED)]) is None


def test_taux_calibre_par_defaut_sans_historique(db):
    assert get_calibrated_qc_rate(db, default=0.8) == 0.8


def test_record_et_recalibrage(db):
    model = Model(name="m", face_reference_url="https://r2/face.jpg")
    db.add(model)
    db.flush()
    job = GenerationJob(model_id=model.id, estimated_cost_usd=10.0)
    db.add(job)
    db.flush()
    for qc_status, actual in [(QcStatus.PASS, 1.2), (QcStatus.PASS, 1.3), (QcStatus.FAIL, 1.1)]:
        db.add(
            JobItem(
                job_id=job.id,
                category="skit",
                combo_hash=uuid.uuid4().hex,
                filled_prompt="p",
                qc_status=qc_status,
                status=ItemStatus.DONE if qc_status == QcStatus.PASS else ItemStatus.FAILED,
                item_actual_cost=actual,
                item_estimated_cost=1.0,
            )
        )
    db.flush()
    db.refresh(job)

    log = record_job_calibration(db, job)
    db.flush()
    assert log.qc_success_rate == pytest.approx(2 / 3)
    # les items fail QC ont quand même coûté la génération → inclus dans le réel
    assert job.actual_cost_usd == pytest.approx(3.6)
    assert get_calibrated_qc_rate(db, default=0.8) == pytest.approx(2 / 3)


def test_plancher_du_taux_calibre(db):
    model = Model(name="m", face_reference_url="u")
    db.add(model)
    db.flush()
    job = GenerationJob(model_id=model.id)
    db.add(job)
    db.flush()
    db.add(CalibrationLog(job_id=job.id, qc_success_rate=0.01))
    db.flush()
    assert get_calibrated_qc_rate(db, default=0.8) == 0.05
