"""Isolation multi-tenant : deux utilisateurs de tenants différents ne voient
ni ne peuvent atteindre les données de l'autre."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.db.base import Base, SessionLocal, engine
from app.db.init_db import SEED_PRICING
from app.db.models import Pricing, User
from app.main import app
from app.services.security import hash_password
from app.workers import tasks as wt


@pytest.fixture(scope="module")
def clients():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        for model, resolution, with_ref, unit, rate in SEED_PRICING:
            db.add(
                Pricing(
                    model=model, resolution=resolution, with_ref=with_ref, unit=unit, rate_usd=rate
                )
            )
        db.add(User(email="a@example.com", password_hash=hash_password("pw-aaaaaaaa"), tenant_id="tenant-a"))
        db.add(User(email="b@example.com", password_hash=hash_password("pw-bbbbbbbb"), tenant_id="tenant-b"))
        db.commit()
    ca, cb = TestClient(app), TestClient(app)
    ca.post("/api/auth/login", json={"email": "a@example.com", "password": "pw-aaaaaaaa"})
    cb.post("/api/auth/login", json={"email": "b@example.com", "password": "pw-bbbbbbbb"})
    return ca, cb


def test_model_isole_par_tenant(clients):
    ca, cb = clients
    mid = ca.post("/api/models", json={"name": "A", "face_reference_url": "https://r2/a.jpg"}).json()["id"]

    # B ne voit pas le model de A
    assert [m["id"] for m in cb.get("/api/models").json()] == []
    # ...et ne peut pas y accéder directement (404, pas 403 → on ne révèle rien)
    assert cb.get(f"/api/models/{mid}").status_code == 404
    # A le voit bien
    assert [m["id"] for m in ca.get("/api/models").json()] == [mid]


def test_banques_isolees(clients):
    ca, cb = clients
    oid = ca.post("/api/banks/outfits", json={"image_url": "https://r2/o.jpg", "tags": ["x"]}).json()["id"]
    assert cb.get("/api/banks/outfits").json() == []
    # B ne peut pas supprimer l'outfit de A
    assert cb.delete(f"/api/banks/outfits/{oid}").status_code == 404
    assert len(ca.get("/api/banks/outfits").json()) == 1


def test_job_isole_et_review_cross_tenant_refuse(clients):
    ca, cb = clients
    mid = ca.post("/api/models", json={"name": "A2", "face_reference_url": "https://r2/a2.jpg"}).json()["id"]
    with patch("app.api.routers.jobs.compose_job"):
        job = ca.post("/api/jobs/batch", json={"model_id": mid, "counts_per_category": {"skit": 1}}).json()

    # B ne voit pas le job de A et ne peut ni le lire ni l'exporter
    assert cb.get("/api/jobs").json() == []
    assert cb.get(f"/api/jobs/{job['id']}").status_code == 404
    assert cb.get(f"/api/jobs/{job['id']}/export").status_code == 404
    assert ca.get(f"/api/jobs/{job['id']}").status_code == 200


def test_compose_n_utilise_que_les_banques_du_tenant(clients):
    ca, cb = clients
    # A a un template+assets ; B n'a rien pour skit
    mid = ca.post("/api/models", json={"name": "A3", "face_reference_url": "https://r2/a3.jpg"}).json()["id"]
    ca.post("/api/banks/templates", json={"category": "skit", "template_text": "scene {outfit}"})
    ca.post("/api/banks/outfits", json={"image_url": "https://r2/oa.jpg", "tags": ["dress"]})

    with patch("app.api.routers.jobs.compose_job"):
        job = ca.post("/api/jobs/batch", json={"model_id": mid, "counts_per_category": {"skit": 1}}).json()
    with patch.object(wt.estimate_and_gate, "delay", side_effect=lambda j: wt.estimate_and_gate(j)):
        with patch.object(wt.dispatch_seedance, "delay"):
            wt.compose_job(job["id"])
    detail = ca.get(f"/api/jobs/{job['id']}").json()
    assert len(detail["items"]) == 1  # composé depuis les banques de A

    # B lance un job skit sans banques → le compose échoue proprement (aucun template)
    mid_b = cb.post("/api/models", json={"name": "B3", "face_reference_url": "https://r2/b3.jpg"}).json()["id"]
    with patch("app.api.routers.jobs.compose_job"):
        jb = cb.post("/api/jobs/batch", json={"model_id": mid_b, "counts_per_category": {"skit": 1}}).json()
    with patch.object(wt.estimate_and_gate, "delay", side_effect=lambda j: wt.estimate_and_gate(j)):
        with patch.object(wt.dispatch_seedance, "delay"):
            wt.compose_job(jb["id"])
    assert cb.get(f"/api/jobs/{jb['id']}").json()["status"] == "failed"
