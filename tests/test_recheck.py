"""Recheck à la demande : pull des résultats kie.ai sans dépendre du webhook."""

import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.db.base import Base, SessionLocal, engine
from app.db.models import ItemStatus, Model, PictureItem, PictureJob, User
from app.integrations.kie import KieTaskResult
from app.main import app
from app.services.security import hash_password

EMAIL = "rc@example.com"
PW = "recheck-pass-1"


def _result(state, urls=None):
    return KieTaskResult(
        task_id="task-1", state=state, result_urls=urls or [], fail_msg=None,
        cost_time=None, cost_usd=0.09, raw={},
    )


@pytest.fixture(scope="module")
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add(User(email=EMAIL, password_hash=hash_password(PW), tenant_id="tnt-rc"))
        db.commit()
    c = TestClient(app)
    c.post("/api/auth/login", json={"email": EMAIL, "password": PW})
    return c


def _make_dispatched_job(tenant="tnt-rc"):
    with SessionLocal() as db:
        m = Model(tenant_id=tenant, name="M", face_reference_url="https://pub.r2.dev/f.jpg")
        db.add(m); db.flush()
        job = PictureJob(tenant_id=tenant, model_id=m.id, status="dispatched", count=1)
        db.add(job); db.flush()
        item = PictureItem(job_id=job.id, combo_hash="h", filled_prompt="p",
                           status=ItemStatus.DISPATCHED, kie_task_id="task-1")
        db.add(item); db.commit()
        return str(job.id), str(item.id)


def test_recheck_pull_le_resultat_et_avance_litem(client):
    job_id, item_id = _make_dispatched_job()
    with patch("app.workers.picture_tasks.kie.get_task",
               return_value=_result("success", ["https://kie.example/out.png"])), \
         patch("app.workers.picture_tasks.process_picture_generated.delay") as proc:
        r = client.post(f"/api/pictures/jobs/{job_id}/recheck")
    assert r.status_code == 200, r.text
    proc.assert_called_once_with(item_id)  # enchaîne le post-traitement
    with SessionLocal() as db:
        it = db.get(PictureItem, uuid.UUID(item_id))
        assert it.status == ItemStatus.GENERATED
        assert it.raw_image_url == "https://kie.example/out.png"
        assert it.item_actual_cost == 0.09


def test_recheck_echec_definitif_marque_failed(client):
    # « sensitive » = refus définitif → pas de retry, direct FAILED
    job_id, item_id = _make_dispatched_job()
    res = KieTaskResult(task_id="t", state="fail", result_urls=[],
                        fail_msg="The input or output was flagged as sensitive.",
                        cost_time=None, cost_usd=None, raw={})
    with patch("app.workers.picture_tasks.kie.get_task", return_value=res):
        assert client.post(f"/api/pictures/jobs/{job_id}/recheck").status_code == 200
    with SessionLocal() as db:
        assert db.get(PictureItem, uuid.UUID(item_id)).status == ItemStatus.FAILED


def test_recheck_echec_transitoire_re_essaie(client):
    # « internal error, try again later » = transitoire → re-dispatch auto, PAS failed
    job_id, item_id = _make_dispatched_job()
    res = KieTaskResult(task_id="t", state="fail", result_urls=[],
                        fail_msg="internal error, please try again later.",
                        cost_time=None, cost_usd=None, raw={})
    with patch("app.workers.picture_tasks.kie.get_task", return_value=res), \
         patch("app.workers.picture_tasks.dispatch_nano_banana.apply_async") as redispatch:
        client.post(f"/api/pictures/jobs/{job_id}/recheck")
    redispatch.assert_called_once()  # ré-essai programmé
    with SessionLocal() as db:
        it = db.get(PictureItem, uuid.UUID(item_id))
        assert it.status == ItemStatus.COMPOSED  # remis en file, pas failed
        assert it.generation_attempts == 1


def test_is_transient_failure():
    from app.integrations.kie import is_transient_failure
    assert is_transient_failure("internal error, please try again later.") is True
    assert is_transient_failure("Rate limit exceeded") is True
    assert is_transient_failure("The input was flagged as sensitive") is False
    assert is_transient_failure("model name not supported") is False
    assert is_transient_failure(None) is False


def test_recheck_cross_tenant_refuse(client):
    job_id, _ = _make_dispatched_job(tenant="autre")
    assert client.post(f"/api/pictures/jobs/{job_id}/recheck").status_code == 404
