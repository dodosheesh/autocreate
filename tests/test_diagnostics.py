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
         patch("app.api.routers.diagnostics._workers", return_value=["celery@w1"]), \
         patch("app.api.routers.diagnostics._probe_url",
               return_value={"status": 200, "width": 1024, "height": 1024, "seedance_ok": True}):
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
         patch("app.api.routers.diagnostics._workers", return_value=[]), \
         patch("app.api.routers.diagnostics._probe_url",
               return_value={"status": "unreachable", "error": "403"}):
        d = client.get("/api/diagnostics").json()
    assert d["workers_online"] == 0  # signale l'absence de worker
    assert d["images_public_ok"] is False  # unreachable → images pas publiques


def test_diagnostics_detecte_image_trop_petite_pour_seedance(client):
    with patch("app.api.routers.diagnostics._broker_ok", return_value=True), \
         patch("app.api.routers.diagnostics._workers", return_value=["w1"]), \
         patch("app.api.routers.diagnostics._probe_url",
               return_value={"status": 200, "width": 180, "height": 180, "seedance_ok": False}):
        d = client.get("/api/diagnostics").json()
    assert d["images_public_ok"] is True  # téléchargeable
    assert d["seedance_dimension_issues"]  # mais hors 300–6000 px → signalé
    assert any("180×180" in v for v in d["seedance_dimension_issues"].values())


def test_reference_dimensions_pointe_l_image_fautive(client):
    # une image trop petite (face) + le reste OK → on veut la nommer précisément
    def fake_probe(url):
        if "f.jpg" in url:  # le visage
            return {"status": 200, "width": 200, "height": 200, "seedance_ok": False}
        return {"status": 200, "width": 1024, "height": 1024, "seedance_ok": True}

    with patch("app.api.routers.diagnostics._probe_url", side_effect=fake_probe):
        d = client.get("/api/diagnostics/reference-dimensions").json()
    assert d["total_checked"] == 4  # 1 face + 1 caractéristique + 2 outfits
    assert d["problems_count"] == 1
    p = d["problems"][0]
    assert p["type"] == "Visage model" and p["label"] == "M"
    assert p["width"] == 200 and p["seedance_ok"] is False


def test_reference_dimensions_signale_non_telechargeable(client):
    with patch("app.api.routers.diagnostics._probe_url",
               return_value={"status": "unreachable", "error": "403"}):
        d = client.get("/api/diagnostics/reference-dimensions").json()
    assert d["problems_count"] == d["total_checked"]  # toutes en échec de download


def test_diagnostics_exige_auth():
    anon = TestClient(app)
    assert anon.get("/api/diagnostics").status_code == 401
    assert anon.get("/api/diagnostics/reference-dimensions").status_code == 401
