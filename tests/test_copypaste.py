"""Feature copypaste (vidéo → vidéo) : prompt fixe, banque vidéo, création de
jobs (vidéo unique / pioche banque / budget), payload Seedance avec vidéo."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import uuid

from app.db.base import Base, SessionLocal, engine
from app.db.init_db import SEED_PRICING
from app.db.models import JobItem, Pricing, User
from app.integrations.kie import build_seedance_input
from app.main import app
from app.services.copypaste import HARD_PROMPT, build_copypaste_prompt
from app.services.security import hash_password

ADMIN_EMAIL = "cp-owner@example.com"
ADMIN_PASSWORD = "test-password-123"


@pytest.fixture(autouse=True)
def _no_probe(monkeypatch):
    """Par défaut les tests ne sondent pas les URLs factices (pas de DNS/ffprobe) ;
    les tests de durée re-patchent avec une valeur précise."""
    monkeypatch.setattr(
        "app.api.routers.copypaste.probe_video_duration", lambda url: None
    )


# ---------- unités ----------


def test_probe_video_duration_parse(monkeypatch):
    import app.media.probe as probe

    class Proc:
        returncode = 0
        stdout = "12.48\n"
        stderr = ""

    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k: Proc())
    assert probe.probe_video_duration("/tmp/x.mp4") == pytest.approx(12.48)

    class Bad:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k: Bad())
    assert probe.probe_video_duration("/tmp/x.mp4") is None


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
    with patch.object(wt.kie, "create_task", return_value="task-cp-1") as create:
        wt.dispatch_seedance(item_id)
    model_slug, payload = create.call_args.args
    # copypaste par défaut = Seedance STANDARD (meilleure qualité)
    assert model_slug == "bytedance/seedance-2"
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


def test_job_save_to_bank_false_ne_pollue_pas_la_banque(client, model_id):
    # (la banque vient d'être vidée par le test précédent)
    with patch("app.api.routers.copypaste.dispatch_seedance"):
        r = client.post(
            "/api/copypaste/jobs",
            json={
                "model_id": model_id,
                "count": 2,
                "reference_video_url": "https://r2.example/videos/test-quality.mp4",
                "save_to_bank": False,
            },
        )
    assert r.status_code == 200, r.text
    # les items utilisent bien la vidéo…
    assert all(
        it["reference_video_url"] == "https://r2.example/videos/test-quality.mp4"
        for it in r.json()["items"]
    )
    # …mais elle n'entre PAS dans la banque
    assert client.get("/api/copypaste/videos").json() == []


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


def test_job_assets_aleatoires_injecte_caracteristique_et_outfit(client, model_id):
    client.post(
        f"/api/models/{model_id}/characteristics",
        json={
            "label": "tattoo",
            "reference_image_url": "https://r2.example/tattoo.jpg",
            "injection_hint": "a floral tattoo on her forearm",
        },
    )
    client.post(
        "/api/banks/outfits",
        json={"image_url": "https://r2.example/outfit.jpg", "tags": ["red dress"]},
    )
    with patch("app.api.routers.copypaste.dispatch_seedance"):
        r = client.post(
            "/api/copypaste/jobs",
            json={
                "model_id": model_id,
                "count": 2,
                "reference_video_url": "https://r2.example/videos/ref6.mp4",
            },
        )
    assert r.status_code == 200, r.text
    job = r.json()
    for it in job["items"]:
        assert "wearing red dress" in it["filled_prompt"]
        assert "floral tattoo" in it["filled_prompt"]
        assert "ackground" not in it["filled_prompt"]  # jamais de background
    # refs : visage d'abord, puis photo du trait, puis photo outfit
    with SessionLocal() as db:
        items = db.query(JobItem).filter(JobItem.job_id == uuid.UUID(job["id"])).all()
        for item in items:
            assert item.reference_image_urls[0] == "https://r2.example/face.jpg"
            assert "https://r2.example/tattoo.jpg" in item.reference_image_urls
            assert "https://r2.example/outfit.jpg" in item.reference_image_urls
            assert item.outfit_id is not None
            assert item.characteristic_ids


def test_resolution_1080p_rejetee_sur_seedance_fast(client, model_id):
    # Qualité « fast » (bytedance/seedance-2-fast) → pas de 1080p.
    r = client.post(
        "/api/copypaste/jobs",
        json={
            "model_id": model_id,
            "count": 1,
            "resolution": "1080p",
            "seedance_quality": "fast",
            "reference_video_url": "https://r2.example/videos/ref8.mp4",
        },
    )
    assert r.status_code == 422
    assert "1080p" in r.json()["detail"]


def test_resolution_1080p_acceptee_en_standard(client, model_id):
    # Qualité « standard » (défaut de la feature) → 1080p OK.
    with patch("app.api.routers.copypaste.dispatch_seedance"):
        r = client.post(
            "/api/copypaste/jobs",
            json={
                "model_id": model_id,
                "count": 1,
                "resolution": "1080p",
                "reference_video_url": "https://r2.example/videos/ref9.mp4",
            },
        )
    assert r.status_code == 200, r.text


def test_batch_1080p_rejete_sur_seedance_fast(client, model_id):
    r = client.post(
        "/api/jobs/batch",
        json={"model_id": model_id, "counts_per_category": {"skit": 1}, "resolution": "1080p"},
    )
    assert r.status_code == 422
    assert "1080p" in r.json()["detail"]


def test_video_trop_longue_rejetee_et_exclue_des_tirages(client, model_id, monkeypatch):
    # banque vidée pour un scénario déterministe
    for v in client.get("/api/copypaste/videos").json():
        client.delete(f"/api/copypaste/videos/{v['id']}")
    monkeypatch.setattr(
        "app.api.routers.copypaste.probe_video_duration", lambda url: 22.0
    )
    # l'ajout en banque sonde et mémorise la durée
    added = client.post(
        "/api/copypaste/videos", json={"video_url": "https://r2.example/videos/long.mp4"}
    ).json()
    assert added["duration_s"] == 22.0
    # vidéo directe > 15 s → refus clair AVANT kie.ai
    r = client.post(
        "/api/copypaste/jobs",
        json={
            "model_id": model_id,
            "count": 1,
            "reference_video_url": "https://r2.example/videos/long.mp4",
        },
    )
    assert r.status_code == 422
    assert "15" in r.json()["detail"]
    # use_bank : la seule vidéo de la banque est trop longue → 409 explicite
    r = client.post(
        "/api/copypaste/jobs", json={"model_id": model_id, "count": 1, "use_bank": True}
    )
    assert r.status_code == 409
    assert "dépassent" in r.json()["detail"]


def test_job_sans_assets_aleatoires(client, model_id):
    with patch("app.api.routers.copypaste.dispatch_seedance"):
        r = client.post(
            "/api/copypaste/jobs",
            json={
                "model_id": model_id,
                "count": 1,
                "reference_video_url": "https://r2.example/videos/ref7.mp4",
                "add_random_assets": False,
            },
        )
    assert r.status_code == 200, r.text
    it = r.json()["items"][0]
    assert "wearing" not in it["filled_prompt"]
    assert "tattoo" not in it["filled_prompt"]
    with SessionLocal() as db:
        item = db.query(JobItem).filter(JobItem.job_id == uuid.UUID(r.json()["id"])).first()
        assert item.reference_image_urls == ["https://r2.example/face.jpg"]
