"""Endpoint de diagnostic : santé worker/broker + URLs de référence cassées."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.db.base import Base, SessionLocal, engine
from app.db.models import Model, ModelCharacteristic, Outfit, User
from app.main import app
from app.services.security import hash_password

EMAIL = "diag@example.com"
PW = "diag-pass-1"


@pytest.fixture(scope="module")
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add(User(email=EMAIL, password_hash=hash_password(PW), tenant_id="tnt-diag"))
        m = Model(tenant_id="tnt-diag", name="M", face_reference_url="https://REMPLACER.r2.dev/f.jpg")
        db.add(m)
        db.flush()
        db.add(ModelCharacteristic(model_id=m.id, label="t",
                                   reference_image_url="https://pub-abc.r2.dev/c.jpg", injection_hint="x"))
        db.add(Outfit(tenant_id="tnt-diag", image_url="https://REMPLACER.r2.dev/o.jpg", tags=["x"]))
        db.add(Outfit(tenant_id="tnt-diag", image_url="https://pub-abc.r2.dev/ok.jpg", tags=["y"]))
        db.commit()
    c = TestClient(app)
    c.post("/api/auth/login", json={"email": EMAIL, "password": PW})
    return c


def test_diagnostics_detecte_urls_cassees_et_etat_worker(client):
    with patch("app.api.routers.diagnostics._broker_ok", return_value=True), \
         patch("app.api.routers.diagnostics._workers", return_value=["celery@w1"]):
        r = client.get("/api/diagnostics")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["broker_reachable"] is True
    assert d["workers_online"] == 1
    assert d["broken_reference_urls"]["model_faces"] == 1  # REMPLACER
    assert d["broken_reference_urls"]["characteristics"] == 0  # pub-abc OK
    assert d["broken_reference_urls"]["outfits"] == 1  # 1 cassé / 2
    assert d["broken_total"] == 2


def test_diagnostics_worker_absent(client):
    with patch("app.api.routers.diagnostics._broker_ok", return_value=True), \
         patch("app.api.routers.diagnostics._workers", return_value=[]):
        d = client.get("/api/diagnostics").json()
    assert d["workers_online"] == 0  # signale l'absence de worker


def test_diagnostics_exige_auth():
    anon = TestClient(app)
    assert anon.get("/api/diagnostics").status_code == 401
