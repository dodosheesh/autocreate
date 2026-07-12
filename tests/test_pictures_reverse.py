"""Reverse-engineering photo SYNCHRONE (endpoint /pictures/prompts)."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.db.base import Base, SessionLocal, engine
from app.db.models import User
from app.main import app
from app.services.security import hash_password

EMAIL = "pic@example.com"
PW = "pic-reverse-1"


@pytest.fixture(scope="module")
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add(User(email=EMAIL, password_hash=hash_password(PW), tenant_id="tnt-pic"))
        db.commit()
    c = TestClient(app)
    c.post("/api/auth/login", json={"email": EMAIL, "password": PW})
    return c


def test_reverse_prompt_synchrone_ready(client):
    with patch(
        "app.api.routers.pictures._vision_reverse", return_value="a cinematic portrait, soft light"
    ) as mock:
        r = client.post(
            "/api/pictures/prompts", json={"source_image_url": "https://r2.example/ref.jpg"}
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ready"  # immédiat, pas de pending
    assert body["prompt_text"] == "a cinematic portrait, soft light"
    assert mock.call_count == 1


def test_reverse_prompt_echec_expose_lerreur(client):
    with patch(
        "app.api.routers.pictures._vision_reverse",
        side_effect=RuntimeError("Claude vision HTTP 400 : could not fetch image"),
    ):
        r = client.post(
            "/api/pictures/prompts", json={"source_image_url": "https://r2.example/bad.jpg"}
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "failed"
    assert "could not fetch image" in (body["error"] or "")


def test_reverse_prompt_bulk_traite_toutes_les_images(client):
    urls = [f"https://r2.example/bulk{i}.jpg" for i in range(3)]

    def fake_reverse(url, _desc):
        if "bulk1" in url:  # une image échoue → n'affecte pas les autres
            raise RuntimeError("vision HTTP 400 : blocked")
        return f"prompt pour {url}"

    with patch("app.api.routers.pictures._vision_reverse", side_effect=fake_reverse) as mock:
        r = client.post("/api/pictures/prompts/bulk", json={"source_image_urls": urls})
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == 3
    assert mock.call_count == 3  # chaque image analysée
    ready = [i for i in items if i["status"] == "ready"]
    failed = [i for i in items if i["status"] == "failed"]
    assert len(ready) == 2 and len(failed) == 1
    assert "blocked" in (failed[0]["error"] or "")


def test_reverse_prompt_bulk_exige_au_moins_une_url(client):
    r = client.post("/api/pictures/prompts/bulk", json={"source_image_urls": []})
    assert r.status_code == 422


def test_delete_prompt_utilise_par_une_photo(client):
    # Un prompt référencé par une photo déjà générée doit quand même se supprimer
    # (la FK est détachée d'abord ; pas de 500 IntegrityError).
    from app.db.models import Model, PictureItem, PictureJob, PicturePrompt

    with SessionLocal() as db:
        p = PicturePrompt(tenant_id="tnt-pic", source_image_url="https://r2.example/used.jpg",
                          prompt_text="a woman", status="ready")
        m = Model(tenant_id="tnt-pic", name="M", face_reference_url="https://r2.example/f.jpg")
        db.add_all([p, m]); db.flush()
        job = PictureJob(tenant_id="tnt-pic", model_id=m.id, count=1)
        db.add(job); db.flush()
        it = PictureItem(job_id=job.id, prompt_id=p.id, combo_hash="z", filled_prompt="a woman")
        db.add(it); db.commit()
        pid, iid = p.id, it.id

    r = client.delete(f"/api/pictures/prompts/{pid}")
    assert r.status_code == 200, r.text
    with SessionLocal() as db:
        from app.db.models import PictureItem as PI, PicturePrompt as PP
        assert db.get(PP, pid) is None            # prompt supprimé
        assert db.get(PI, iid).prompt_id is None    # référence photo détachée
