"""Test d'intégration API bout en bout sur SQLite : banques → batch →
compose/gate (tâches Celery exécutées inline) → webhook → review → export.
Aucun appel réseau : dispatch kie.ai et process mockés."""

import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.db.base import Base, SessionLocal, engine
from app.db.init_db import SEED_PRICING
from app.db.models import GenerationJob, ItemStatus, JobItem, Pricing, User
from app.main import app
from app.services.security import hash_password
from app.workers import tasks as wt

ADMIN_EMAIL = "owner@example.com"
ADMIN_PASSWORD = "test-password-123"


@pytest.fixture(scope="module")
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        for model, resolution, with_ref, unit, rate in SEED_PRICING:
            db.add(
                Pricing(
                    model=model, resolution=resolution, with_ref=with_ref, unit=unit, rate_usd=rate
                )
            )
        db.add(User(email=ADMIN_EMAIL, password_hash=hash_password(ADMIN_PASSWORD), role="owner"))
        db.commit()
    c = TestClient(app)
    # Authentifie le client pour toute la session de test (cookie persistant)
    r = c.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture(scope="module")
def model_id(client):
    r = client.post(
        "/api/models",
        json={"name": "Test", "face_reference_url": "https://r2.example/face.jpg"},
    )
    mid = r.json()["id"]
    client.post(
        f"/api/models/{mid}/characteristics",
        json={
            "label": "tatouage",
            "reference_image_url": "https://r2.example/tattoo.jpg",
            "injection_hint": "a floral tattoo on her left forearm",
        },
    )
    return mid


@pytest.fixture(scope="module")
def banks(client):
    client.post("/api/banks/outfits", json={"image_url": "https://r2.example/o1.jpg", "tags": ["red dress"]})
    client.post("/api/banks/outfits", json={"image_url": "https://r2.example/o2.jpg", "tags": ["denim"]})
    client.post("/api/banks/backgrounds", json={"image_url": "https://r2.example/b1.jpg", "tags": ["beach"]})
    client.post(
        "/api/banks/templates",
        json={
            "category": "podcast",
            "template_text": "Podcast set, {outfit}, {background}, {characteristics}. {dialogue}",
            "speaking": True,
        },
    )
    client.post(
        "/api/banks/dialogues",
        json={"category": "podcast", "raw_text": "[H] line one\n[F] line two"},
    )


def _run_compose_inline(job_id: str) -> None:
    with patch.object(wt.estimate_and_gate, "delay", side_effect=lambda jid: wt.estimate_and_gate(jid)):
        with patch.object(wt.dispatch_seedance, "delay"):
            wt.compose_job(job_id)


def test_full_batch_flow(client, model_id, banks):
    # 1. estimation live multi-catégories (podcast speaking → coût voix inclus)
    r = client.post(
        "/api/estimate/batch",
        json={"counts_per_category": {"podcast": 10}, "duration_s": 10, "resolution": "720p"},
    )
    assert r.status_code == 200
    assert r.json()["gross_usd"] == pytest.approx(12.6667, abs=0.01)

    # 2. batch → compose inline → dispatched
    with patch("app.api.routers.jobs.compose_job"):
        job = client.post(
            "/api/jobs/batch",
            json={"model_id": model_id, "counts_per_category": {"podcast": 2}},
        ).json()
    _run_compose_inline(job["id"])
    detail = client.get(f"/api/jobs/{job['id']}").json()
    assert detail["status"] == "dispatched"
    assert len(detail["items"]) == 2
    assert all("floral tattoo" in i["filled_prompt"] for i in detail["items"])

    # 3. webhook succès (avec coût réel) sur le premier item
    with SessionLocal() as db:
        items = db.query(JobItem).filter(JobItem.job_id == uuid.UUID(job["id"])).all()
        for n, item in enumerate(items):
            item.seedance_task_id = f"itest_{n}"
            item.status = ItemStatus.DISPATCHED
        db.commit()
        first_id = str(items[0].id)
    with patch.object(wt.process_generated, "delay"):
        r = client.post(
            "/api/webhooks/kie?secret=test-webhook-secret",
            json={
                "code": 200,
                "data": {
                    "taskId": "itest_0",
                    "state": "success",
                    "resultJson": '{"resultUrls": ["https://kie/v0.mp4"]}',
                    "costUsd": 1.19,
                },
            },
        )
    assert r.json()["ok"] is True

    # 4. le second échoue ; le premier termine → job finalisé + calibration
    client.post(
        "/api/webhooks/kie?secret=test-webhook-secret",
        json={"code": 200, "data": {"taskId": "itest_1", "state": "fail", "failMsg": "x"}},
    )
    with wt.db_session() as db:
        item = db.get(JobItem, uuid.UUID(first_id))
        item.status = ItemStatus.DONE
        item.final_video_url = "https://r2/final.mp4"
        wt._finalize_job_if_done(db, item.job)
    with SessionLocal() as db:
        assert db.get(GenerationJob, uuid.UUID(job["id"])).status == "completed"

    # 5. review + export
    r = client.post(f"/api/items/{first_id}/review", json={"decision": "approved"})
    assert r.json()["review_status"] == "approved"
    export = client.get(f"/api/jobs/{job['id']}/export").json()
    assert export["approved_count"] == 1
    assert export["videos"][0]["url"] == "https://r2/final.mp4"


def test_budget_gate_bloque_avant_dispatch(client, model_id, banks):
    with patch("app.api.routers.jobs.compose_job"):
        job = client.post(
            "/api/jobs/batch",
            json={
                "model_id": model_id,
                "counts_per_category": {"podcast": 2},
                "budget_cap_usd": 1.0,
            },
        ).json()
    _run_compose_inline(job["id"])
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "blocked_budget"


def test_ui_servie(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Generate" in r.text


def test_routes_protegees_sans_session():
    anon = TestClient(app)
    assert anon.get("/api/models").status_code == 401
    assert anon.get("/api/jobs").status_code == 401
    assert anon.post("/api/estimate/batch", json={"counts_per_category": {"skit": 1}}).status_code == 401


def test_login_mauvais_mot_de_passe():
    anon = TestClient(app)
    r = anon.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": "faux"})
    assert r.status_code == 401


def test_login_puis_me_et_logout():
    c = TestClient(app)
    assert c.get("/api/auth/me").status_code == 401
    c.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    me = c.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["email"] == ADMIN_EMAIL
    c.post("/api/auth/logout")
    assert c.get("/api/auth/me").status_code == 401


def test_change_password_revoque_les_anciennes_sessions(client):
    """Le token_version invalide un cookie volé après changement de mdp."""
    stolen = TestClient(app)
    stolen.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert stolen.get("/api/auth/me").status_code == 200  # session active

    # le propriétaire change son mot de passe depuis une autre session (le fixture client)
    r = client.post(
        "/api/auth/change-password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "nouveau-mdp-123"},
    )
    assert r.status_code == 200
    # l'ancien cookie (token_version périmé) est désormais rejeté
    assert stolen.get("/api/auth/me").status_code == 401
    # remet le mot de passe d'origine pour ne pas polluer les autres tests
    client.post(
        "/api/auth/change-password",
        json={"current_password": "nouveau-mdp-123", "new_password": ADMIN_PASSWORD},
    )


def test_webhook_refuse_sans_secret_ou_mauvais_secret(client):
    # sans secret → 401 (secret configuré mais absent de la requête)
    r = client.post("/api/webhooks/kie", json={"data": {"taskId": "x", "state": "success"}})
    assert r.status_code == 401
    # mauvais secret → 401
    r = client.post(
        "/api/webhooks/kie?secret=faux", json={"data": {"taskId": "x", "state": "success"}}
    )
    assert r.status_code == 401


def test_url_non_http_rejettee_a_l_entree(client):
    r = client.post(
        "/api/models", json={"name": "x", "face_reference_url": "file:///etc/passwd"}
    )
    assert r.status_code == 422
