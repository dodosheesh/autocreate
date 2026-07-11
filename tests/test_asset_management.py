"""Auto-description (synchrone) + reprise des pending + gestion des assets."""

import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.db.base import Base, SessionLocal, engine
from app.db.models import Background, GenerationJob, Model, Outfit, User
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


def test_bulk_describe_synchrone_ready_avec_suffixe(client):
    # La description est SYNCHRONE (aucun worker Celery requis) : les lignes
    # reviennent déjà "ready" avec la phrase + le suffixe utilisateur.
    with patch(
        "app.api.routers.banks._vision_describe", return_value="a red silk mini dress"
    ) as mock:
        r = client.post(
            "/api/banks/outfits/bulk-describe",
            json={
                "image_urls": ["https://r2.example/o1.jpg", "https://r2.example/o2.jpg"],
                "suffix": "with matching accessories",
            },
        )
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 2
    assert all(o["status"] == "ready" for o in rows)
    assert all(o["tags"] == ["a red silk mini dress with matching accessories"] for o in rows)
    assert mock.call_count == 2  # une analyse vision par image


def test_bulk_describe_une_image_en_echec_ne_bloque_pas_les_autres(client):
    calls = {"n": 0}

    def flaky(url, kind):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("vision HTTP 500")
        return "a neon-lit street"

    with patch("app.api.routers.banks._vision_describe", side_effect=flaky):
        r = client.post(
            "/api/banks/backgrounds/bulk-describe",
            json={"image_urls": ["https://r2.example/b1.jpg", "https://r2.example/b2.jpg"]},
        )
    assert r.status_code == 200, r.text
    rows = r.json()
    statuses = sorted(o["status"] for o in rows)
    assert statuses == ["failed", "ready"]  # l'échec est isolé
    failed = next(o for o in rows if o["status"] == "failed")
    assert "vision HTTP 500" in (failed["error"] or "")  # raison visible dans l'UI


def test_describe_pending_relance_les_bloques(client):
    # Simule des lignes restées "pending" (importées avant la description synchrone)
    with SessionLocal() as db:
        db.add(Outfit(tenant_id="tnt-am", image_url="https://r2.example/p1.jpg", tags=[], status="pending"))
        db.add(Background(tenant_id="tnt-am", image_url="https://r2.example/p2.jpg", tags=[], status="pending"))
        db.commit()
    with patch("app.api.routers.banks._vision_describe", return_value="a described scene"):
        r = client.post("/api/banks/assets/describe-pending", json={})
    assert r.status_code == 200, r.text
    assert r.json()["described"]["outfit"] >= 1
    assert r.json()["described"]["background"] >= 1
    # plus aucun pending pour ce tenant
    with SessionLocal() as db:
        left = db.query(Outfit).filter(Outfit.tenant_id == "tnt-am", Outfit.status == "pending").count()
        assert left == 0


def test_describe_asset_retry_garde_pending_puis_failed():
    # Le worker Celery reste correct : sur échec transitoire le statut reste
    # "pending" (ré-essai possible) et ne passe "failed" qu'une fois épuisé.
    with SessionLocal() as db:
        o = Outfit(tenant_id="tnt-retry", image_url="u", tags=[], status="pending")
        db.add(o); db.commit(); oid = str(o.id)
    with patch("app.workers.picture_tasks._vision_describe", side_effect=RuntimeError("429")):
        with pytest.raises(Exception):
            pt.describe_asset("outfit", oid, "")
    with SessionLocal() as db:
        assert db.get(Outfit, uuid.UUID(oid)).status == "pending"  # retriable
    # essais épuisés → failed
    pt.describe_asset.push_request(retries=2)
    try:
        with patch("app.workers.picture_tasks._vision_describe", side_effect=RuntimeError("429")):
            pt.describe_asset("outfit", oid, "")
    finally:
        pt.describe_asset.pop_request()
    with SessionLocal() as db:
        assert db.get(Outfit, uuid.UUID(oid)).status == "failed"


def test_describe_asset_row_supprime_pendant_analyse_ne_crashe_pas():
    with SessionLocal() as db:
        o = Outfit(tenant_id="tnt-race", image_url="u", tags=[], status="pending")
        db.add(o); db.commit(); oid = str(o.id)

    def delete_then_return(url, kind):
        with SessionLocal() as db:  # suppression concurrente pendant l'appel vision
            db.delete(db.get(Outfit, uuid.UUID(oid)))
            db.commit()
        return "a phrase"

    with patch("app.workers.picture_tasks._vision_describe", side_effect=delete_then_return):
        pt.describe_asset("outfit", oid, "")  # ne doit PAS lever d'AttributeError


def test_describe_asset_ajoute_desc_et_suffixe():
    with SessionLocal() as db:
        o = Outfit(tenant_id="tnt-am2", image_url="https://r2.example/x.jpg", tags=[], status="pending")
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


def test_delete_model_utilise_par_un_job_refuse(client):
    mid = client.post("/api/models", json={"name": "Used", "face_reference_url": "https://r2.example/f.jpg"}).json()["id"]
    with SessionLocal() as db:
        db.add(GenerationJob(tenant_id="tnt-am", model_id=uuid.UUID(mid)))
        db.commit()
    # référencé par un job → 409 (pas de 500 IntegrityError, pas d'effacement silencieux)
    r = client.delete(f"/api/models/{mid}")
    assert r.status_code == 409, r.text
    assert client.get(f"/api/models/{mid}").status_code == 200  # toujours là


def test_patch_model_face_et_caracteristique(client):
    mid = client.post("/api/models", json={"name": "Editable", "face_reference_url": "https://REMPLACER.r2.dev/old.jpg"}).json()["id"]
    cid = client.post(f"/api/models/{mid}/characteristics", json={
        "label": "trait", "reference_image_url": "https://REMPLACER.r2.dev/oldc.jpg", "injection_hint": "a trait"}).json()["id"]
    # remplacer la photo visage (URL cassée → nouvelle URL)
    r = client.patch(f"/api/models/{mid}", json={"face_reference_url": "https://pub-ok.r2.dev/new.jpg"})
    assert r.status_code == 200, r.text
    assert r.json()["face_reference_url"] == "https://pub-ok.r2.dev/new.jpg"
    assert r.json()["name"] == "Editable"  # non touché (exclude_unset)
    # remplacer l'image d'une caractéristique
    r2 = client.patch(f"/api/models/{mid}/characteristics/{cid}",
                      json={"reference_image_url": "https://pub-ok.r2.dev/newc.jpg"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["reference_image_url"] == "https://pub-ok.r2.dev/newc.jpg"


def test_caracteristique_recurring_create_et_toggle(client):
    mid = client.post("/api/models", json={"name": "Rec", "face_reference_url": "https://r2.example/f.jpg"}).json()["id"]
    # créée non-récurrente par défaut
    c = client.post(f"/api/models/{mid}/characteristics", json={
        "label": "piercing", "reference_image_url": "https://r2.example/p.jpg", "injection_hint": "a piercing"}).json()
    assert c["recurring"] is False
    # tattoo créé récurrent
    t = client.post(f"/api/models/{mid}/characteristics", json={
        "label": "tattoo", "reference_image_url": "https://r2.example/t.jpg", "injection_hint": "a tattoo",
        "recurring": True}).json()
    assert t["recurring"] is True
    # bascule le piercing en récurrent puis inverse
    r = client.patch(f"/api/models/{mid}/characteristics/{c['id']}", json={"recurring": True})
    assert r.status_code == 200 and r.json()["recurring"] is True


def test_patch_model_cross_tenant_refuse(client):
    with SessionLocal() as db:
        other = Model(tenant_id="autre", name="X", face_reference_url="u")
        db.add(other); db.commit(); oid = str(other.id)
    assert client.patch(f"/api/models/{oid}", json={"name": "hack"}).status_code == 404


def test_delete_model_cross_tenant_refuse(client):
    with SessionLocal() as db:
        other = Model(tenant_id="autre", name="X", face_reference_url="u")
        db.add(other); db.commit(); oid = str(other.id)
    assert client.delete(f"/api/models/{oid}").status_code == 404
