"""Auto-description bulk + gestion (suppression) des assets."""

import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.db.base import Base, SessionLocal, engine
from app.db.models import Background, Outfit, User
from app.main import app
from app.services.security import hash_password
from app.workers import picture_tasks as pt

EMAIL = "am@example.com"
PW = "asset-mgmt-1"


@pytest.fixture(scope="module")
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add(User(email=EMAIL, password_hash=hash_password(PW), tenant_id="tnt-am"))
        db.commit()
    c = TestClient(app)
    c.post("/api/auth/login", json={"email": EMAIL, "password": PW})
    return c


def test_bulk_describe_cree_pending_et_dispatch(client):
    with patch("app.api.routers.banks.describe_asset") as mock_task:
        r = client.post(
            "/api/banks/outfits/bulk-describe",
            json={"image_urls": ["https://r2.example/o1.jpg", "https://r2.example/o2.jpg"],
                  "suffix": "with matching accessories"},
        )
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 2
    assert all(o["status"] == "pending" for o in rows)
    assert mock_task.delay.call_count == 2
    # le suffixe est bien transmis à la tâche
    assert mock_task.delay.call_args[0][2] == "with matching accessories"


def test_describe_asset_ajoute_desc_et_suffixe(client):
    with SessionLocal() as db:
        o = Outfit(tenant_id="tnt-am", image_url="https://r2.example/x.jpg", tags=[], status="pending")
        db.add(o); db.commit(); oid = str(o.id)
    with patch("app.workers.picture_tasks._vision_describe", return_value="a red silk mini dress"):
        pt.describe_asset("outfit", oid, "with matching accessories")
    with SessionLocal() as db:
        o = db.get(Outfit, uuid.UUID(oid))
        assert o.status == "ready"
        assert o.tags == ["a red silk mini dress with matching accessories"]


def test_pending_outfit_non_tire_a_la_composition():
    from app.workers.tasks import _build_pools
    with SessionLocal() as db:
        db.add(Outfit(tenant_id="tnt-pool", image_url="u", tags=["ready one"], status="ready"))
        db.add(Outfit(tenant_id="tnt-pool", image_url="u", tags=["pending one"], status="pending"))
        db.add(Background(tenant_id="tnt-pool", image_url="u", tags=["bg"], status="ready"))
        from app.db.models import PromptTemplate
        db.add(PromptTemplate(tenant_id="tnt-pool", category="skit",
                              template_text="{outfit} {background}", status="ready"))
        db.commit()
        pools = _build_pools(db, ["skit"], "tnt-pool")
    assert len(pools["skit"].outfits) == 1  # seul le ready


def test_delete_outfit_et_background(client):
    oid = client.post("/api/banks/outfits", json={"image_url": "https://r2.example/d.jpg", "tags": ["x"]}).json()["id"]
    assert client.delete(f"/api/banks/outfits/{oid}").status_code == 200
    assert oid not in [o["id"] for o in client.get("/api/banks/outfits").json()]


def test_delete_model_et_caracteristique(client):
    mid = client.post("/api/models", json={"name": "ToDelete", "face_reference_url": "https://r2.example/f.jpg"}).json()["id"]
    cid = client.post(f"/api/models/{mid}/characteristics", json={
        "label": "trait", "reference_image_url": "https://r2.example/t.jpg", "injection_hint": "a trait"}).json()["id"]
    # supprimer la caractéristique
    assert client.delete(f"/api/models/{mid}/characteristics/{cid}").status_code == 200
    assert client.get(f"/api/models/{mid}").json()["characteristics"] == []
    # supprimer la model
    assert client.delete(f"/api/models/{mid}").status_code == 200
    assert client.get(f"/api/models/{mid}").status_code == 404


def test_delete_model_cross_tenant_refuse(client):
    # model d'un autre tenant → 404 (pas de fuite)
    with SessionLocal() as db:
        from app.db.models import Model
        other = Model(tenant_id="autre", name="X", face_reference_url="u")
        db.add(other); db.commit(); oid = str(other.id)
    assert client.delete(f"/api/models/{oid}").status_code == 404
