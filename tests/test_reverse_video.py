"""Reverse-engineering vidéo → template réutilisable."""

import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.db.base import Base, SessionLocal, engine
from app.db.models import PromptTemplate, User
from app.main import app
from app.services.security import hash_password
from app.services.template_library import ensure_slots
from app.workers import picture_tasks as pt

EMAIL = "rv@example.com"
PW = "reverse-video-1"


# ---------- ensure_slots (pur) ----------


def test_ensure_slots_ajoute_les_manquants():
    out = ensure_slots("A woman dances energetically, handheld tracking shot.", speaking=False)
    assert "{outfit}" in out and "{background}" in out and "{characteristics}" in out
    assert "{dialogue}" not in out


def test_ensure_slots_dialogue_si_speaking():
    out = ensure_slots("She talks to camera. {outfit} {background} {characteristics}", speaking=True)
    assert "{dialogue}" in out


def test_ensure_slots_preserve_les_slots_presents():
    src = "Scene {outfit} in {background}. {characteristics}."
    out = ensure_slots(src, speaking=False)
    assert out.count("{outfit}") == 1  # pas de duplication


# ---------- endpoint + tâche ----------


@pytest.fixture(scope="module")
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add(User(email=EMAIL, password_hash=hash_password(PW), tenant_id="tnt-rv"))
        db.commit()
    c = TestClient(app)
    c.post("/api/auth/login", json={"email": EMAIL, "password": PW})
    return c


def test_reverse_video_flow(client):
    # 1. POST crée un template pending + dispatch async
    with patch("app.api.routers.banks.reverse_engineer_video") as mock_task:
        r = client.post(
            "/api/banks/templates/reverse-video",
            json={"source_video_url": "https://r2.example/ref.mp4",
                  "category": "storytelling", "speaking": True},
        )
        assert r.status_code == 200, r.text
        tmpl = r.json()
        assert tmpl["status"] == "pending"
        assert mock_task.delay.call_count == 1

    # 2. exécuter la tâche inline : download + keyframes + vision + transcription mockés
    with patch("app.workers.picture_tasks._download"), \
         patch("app.workers.picture_tasks.extract_keyframes", return_value=["f0.jpg", "f1.jpg"]), \
         patch("app.workers.picture_tasks._transcribe_video", return_value=""), \
         patch(
             "app.workers.picture_tasks._vision_reverse_video",
             return_value="A woman dances energetically down a street, handheld tracking shot, neon night mood.",
         ):
        pt.reverse_engineer_video(tmpl["id"])

    # 3. le template est ready, avec slots garantis + dialogue (speaking)
    got = client.get("/api/banks/templates").json()
    ready = next(t for t in got if t["id"] == tmpl["id"])
    assert ready["status"] == "ready"
    assert "{outfit}" in ready["template_text"]
    assert "{characteristics}" in ready["template_text"]
    assert "{dialogue}" in ready["template_text"]
    assert "handheld tracking shot" in ready["template_text"]


def test_reverse_video_transcrit_la_parole_en_dialogue(client):
    with patch("app.api.routers.banks.reverse_engineer_video"):
        tid = client.post("/api/banks/templates/reverse-video", json={
            "source_video_url": "https://r2.example/talk.mp4",
            "category": "micro_trottoir", "speaking": True}).json()["id"]
    with patch("app.workers.picture_tasks._download"), \
         patch("app.workers.picture_tasks.extract_keyframes", return_value=["f0.jpg"]), \
         patch("app.workers.picture_tasks._vision_reverse_video",
               return_value="A woman answers on the street. {outfit} {background} {characteristics} {dialogue}"), \
         patch("app.workers.picture_tasks._transcribe_video",
               return_value="yo bro let me know man to man"):
        pt.reverse_engineer_video(tid)
    # la parole transcrite est stockée comme ligne de dialogue [F] dans la catégorie
    dialogues = client.get("/api/banks/dialogues?category=micro_trottoir").json()
    assert any(d["raw_text"] == "[F] yo bro let me know man to man" for d in dialogues)


def test_transcribe_audio_appelle_scribe(tmp_path):
    from unittest.mock import MagicMock

    from app.integrations import elevenlabs

    a = tmp_path / "a.wav"
    a.write_bytes(b"RIFFfakeaudio")
    settings = MagicMock(elevenlabs_api_key="k", elevenlabs_base_url="https://api.elevenlabs.io",
                         elevenlabs_stt_model="scribe_v1")
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"text": "  hello world  "}
    with patch("app.integrations.elevenlabs.get_settings", return_value=settings), \
         patch("app.integrations.elevenlabs.httpx.post", return_value=resp) as post:
        out = elevenlabs.transcribe_audio(str(a))
    assert out == "hello world"
    assert post.call_args.kwargs["data"]["model_id"] == "scribe_v1"


def test_reverse_video_bulk_cree_n_templates(client):
    urls = [f"https://r2.example/v{i}.mp4" for i in range(4)]
    with patch("app.api.routers.banks.reverse_engineer_video") as mock_task:
        r = client.post(
            "/api/banks/templates/reverse-video/bulk",
            json={"source_video_urls": urls, "category": "skit", "speaking": False},
        )
    assert r.status_code == 200, r.text
    tmpls = r.json()
    assert len(tmpls) == 4
    assert all(t["status"] == "pending" and t["category"] == "skit" for t in tmpls)
    assert mock_task.delay.call_count == 4  # une analyse async par vidéo


def test_reverse_video_bulk_exige_au_moins_une_url(client):
    r = client.post(
        "/api/banks/templates/reverse-video/bulk",
        json={"source_video_urls": [], "category": "skit"},
    )
    assert r.status_code == 422


def test_pending_template_non_tire_a_la_composition(client):
    # un template pending ne doit pas être utilisable en génération
    from app.workers.tasks import _build_pools

    with SessionLocal() as db:
        db.add(PromptTemplate(
            tenant_id="tnt-rv", category="skit", template_text="{outfit} {background}",
            status="pending", source_video_url="https://r2.example/x.mp4"))
        db.add(PromptTemplate(
            tenant_id="tnt-rv", category="skit", template_text="ready one {outfit} {background}",
            status="ready"))
        db.commit()
        pools = _build_pools(db, ["skit"], "tnt-rv")
    assert len(pools["skit"].templates) == 1  # seul le ready
