"""Feature copypaste (vidéo → vidéo) : prompt fixe, banque vidéo, création de
jobs (vidéo unique / pioche banque / budget), payload Seedance avec vidéo."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.db.base import Base, SessionLocal, engine
from app.db.init_db import SEED_PRICING
from app.db.models import Pricing, User
from app.integrations.kie import build_seedance_input
from app.main import app
from app.services.copypaste import HARD_PROMPT, build_copypaste_prompt
from app.services.security import hash_password

ADMIN_EMAIL = "cp-owner@example.com"
ADMIN_PASSWORD = "test-password-123"


# ---------- unités ----------


def test_build_copypaste_prompt():
    assert build_copypaste_prompt() == HARD_PROMPT
    assert build_copypaste_prompt("   ") == HARD_PROMPT
    full = build_copypaste_prompt("keep the same outfit")
    assert full.startswith(HARD_PROMPT)
    assert full.endswith("keep the same outfit.")
    # ponctuation déjà présente → pas de double point
    assert build_copypaste_prompt("no zoom!").endswith("no zoom!")


def test_seedance_input_avec_video_de_reference():
    payload = build_seedance_input(
        "p", ["face.jpg"], "720p", 10,
        reference_video_urls=["https://r2.example/ref.mp4"],
    )
    assert payload["reference_video_urls"] == ["https://r2.example/ref.mp4"]
    assert payload["reference_image_urls"] == ["face.jpg"]
    # sans vidéo → le champ n'apparaît pas (input image-to-video inchangé)
    assert "reference_video_urls" not in build_seedance_input("p", ["face.jpg"], "720p", 10)


# ---------- API ----------


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
    r = c.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture(scope="module")
def model_id(client):
    r = client.post(
        "/api/models",
        json={"name": "CP", "face_reference_url": "https://r2.example/face.jpg"},
    )
    return r.json()["id"]


VIDEO = "https://r2.example/videos/ref1.mp4"


def test_banque_ajout_idempotent_et_liste(client):
    r1 = client.post("/api/copypaste/videos", json={"video_url": VIDEO, "label": "ref 1"})
    r2 = client.post("/api/copypaste/videos", json={"video_url": VIDEO})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]  # dédup par URL
    assert len(client.get("/api/copypaste/videos").json()) == 1


def test_job_video_unique(client, model_id):
    with patch("app.api.routers.copypaste.dispatch_seedance") as disp:
        r = client.post(
            "/api/copypaste/jobs",
            json={
                "model_id": model_id,
                "count": 3,
                "reference_video_url": "https://r2.example/videos/ref2.mp4",
                "custom_prompt": "keep the same outfit",
            },
        )
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["counts_per_category"] == {"copypaste": 3}
    assert job["estimated_cost_usd"] and job["estimated_cost_usd"] > 0
    assert disp.delay.call_count == 3
    assert len(job["items"]) == 3
    for it in job["items"]:
        assert it["category"] == "copypaste"
        assert it["filled_prompt"].startswith(HARD_PROMPT)
        assert "keep the same outfit" in it["filled_prompt"]
        assert it["reference_video_url"] == "https://r2.example/videos/ref2.mp4"
    # la vidéo du job a rejoint la banque automatiquement
    urls = [v["video_url"] for v in client.get("/api/copypaste/videos").json()]
    assert "https://r2.example/videos/ref2.mp4" in urls


def test_dispatch_envoie_la_video_a_seedance(client, model_id):
    from app.workers import tasks as wt

    with patch("app.api.routers.copypaste.dispatch_seedance"):
        job = client.post(
            "/api/copypaste/jobs",
            json={
                "model_id": model_id,
                "count": 1,
                "reference_video_url": "https://r2.example/videos/ref4.mp4",
            },
        ).json()
    item_id = job["items"][0]["id"]
    with patch.object(wt.kie, "create_seedance_task", return_value="task-cp-1") as create:
        wt.dispatch_seedance(item_id)
    payload = create.call_args.args[0]
    assert payload["reference_video_urls"] == ["https://r2.example/videos/ref4.mp4"]
    assert payload["reference_image_urls"] == ["https://r2.example/face.jpg"]
    assert payload["prompt"] == HARD_PROMPT


def test_job_use_bank_pioche_dans_la_banque(client, model_id):
    with patch("app.api.routers.copypaste.dispatch_seedance"):
        r = client.post(
            "/api/copypaste/jobs",
            json={"model_id": model_id, "count": 5, "use_bank": True},
        )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 5
    bank = {v["video_url"] for v in client.get("/api/copypaste/videos").json()}
    assert all(it["reference_video_url"] in bank for it in items)


def test_job_sans_video_ni_banque_rejette(client, model_id):
    r = client.post("/api/copypaste/jobs", json={"model_id": model_id, "count": 1})
    assert r.status_code == 422


def test_job_use_bank_banque_vide_rejette(client, model_id):
    for v in client.get("/api/copypaste/videos").json():
        client.delete(f"/api/copypaste/videos/{v['id']}")
    r = client.post(
        "/api/copypaste/jobs", json={"model_id": model_id, "count": 1, "use_bank": True}
    )
    assert r.status_code == 409


def test_job_budget_cap_bloque(client, model_id):
    with patch("app.api.routers.copypaste.dispatch_seedance") as disp:
        r = client.post(
            "/api/copypaste/jobs",
            json={
                "model_id": model_id,
                "count": 10,
                "reference_video_url": "https://r2.example/videos/ref5.mp4",
                "budget_cap_usd": 0.01,
            },
        )
    assert r.status_code == 200
    assert r.json()["status"] == "blocked_budget"
    disp.delay.assert_not_called()
