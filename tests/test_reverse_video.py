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


def test_strip_background_slot():
    from app.services.template_library import strip_background_slot

    assert strip_background_slot(
        "A woman sits on a bed in {background}, wearing {outfit}."
    ) == "A woman sits on a bed, wearing {outfit}."
    # {background} isolé aussi retiré, {outfit} préservé
    assert "{background}" not in strip_background_slot("Scene {background}. {outfit}")
    assert "{outfit}" in strip_background_slot("Scene {background}. {outfit}")


def test_ensure_slots_sans_background_pour_format_long():
    # format long 30 s : le décor est celui de la vidéo → PAS de slot {background}
    out = ensure_slots("A woman talks in a cozy bedroom.", speaking=True, with_background=False)
    assert "{outfit}" in out and "{characteristics}" in out and "{dialogue}" in out
    assert "{background}" not in out


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
         patch("app.workers.picture_tasks._transcribe_video", return_value=("", "")), \
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
               return_value=("yo bro let me know man to man", "")):
        pt.reverse_engineer_video(tid)
    # la parole transcrite est stockée comme ligne de dialogue [F] dans la catégorie
    dialogues = client.get("/api/banks/dialogues?category=micro_trottoir").json()
    assert any(d["raw_text"] == "[F] yo bro let me know man to man" for d in dialogues)


def test_reverse_video_long_form_stocke_le_transcript_sans_polluer_la_banque(client):
    # storytelling_long : la scène garde son décor (pas de slot {background}),
    # le transcript est stocké SUR le template, et AUCUNE ligne n'est ajoutée à
    # la banque de dialogues (paroles = reverse uniquement).
    from app.db.models import DialogueLine, PromptTemplate

    with patch("app.api.routers.banks.reverse_engineer_video"):
        tid = client.post("/api/banks/templates/reverse-video", json={
            "source_video_url": "https://r2.example/long.mp4",
            "category": "storytelling_long", "speaking": False}).json()["id"]
    with patch("app.workers.picture_tasks._download"), \
         patch("app.workers.picture_tasks.extract_keyframes", return_value=["f0.jpg"]), \
         patch("app.workers.picture_tasks._vision_reverse_video",
               return_value="A woman tells a story in a warm cozy bedroom at night."), \
         patch("app.workers.picture_tasks._transcribe_video",
               return_value=("So this happened last week. I could not believe it. "
                             "It was wild. Then he showed up. Everything changed.", "")):
        pt.reverse_engineer_video(tid)

    with SessionLocal() as db:
        tmpl = db.get(PromptTemplate, uuid.UUID(tid))
        assert tmpl.status == "ready"
        assert tmpl.speaking is True  # forcé (le format long est parlant)
        # Voix MAJORITAIREMENT masculine [H], de temps en temps féminine [F].
        assert tmpl.transcript.startswith("[H] So this happened")
        h = tmpl.transcript.count("[H]")
        f = tmpl.transcript.count("[F]")
        assert h > f and f >= 1  # majorité masculine, mais au moins une féminine
        assert "{background}" not in tmpl.template_text  # décor baked
        # aucune ligne de dialogue ajoutée à la banque pour ce format
        lines = db.scalars(
            __import__("sqlalchemy").select(DialogueLine).where(
                DialogueLine.category == "storytelling_long")
        ).all()
        assert lines == []


def test_reverse_video_long_form_echoue_si_pas_de_paroles(client):
    # storytelling_long sans transcript (transcription échouée) → le template est
    # marqué FAILED avec la raison, pas laissé « ready » mais inutilisable.
    from app.db.models import PromptTemplate

    with patch("app.api.routers.banks.reverse_engineer_video"):
        tid = client.post("/api/banks/templates/reverse-video", json={
            "source_video_url": "https://r2.example/silent.mp4",
            "category": "storytelling_long", "speaking": True}).json()["id"]
    with patch("app.workers.picture_tasks._download"), \
         patch("app.workers.picture_tasks.extract_keyframes", return_value=["f0.jpg"]), \
         patch("app.workers.picture_tasks._vision_reverse_video",
               return_value="A woman tells a story in a cozy bedroom."), \
         patch("app.workers.picture_tasks._transcribe_video",
               return_value=("", "transcription ElevenLabs échouée : HTTP 401")):
        pt.reverse_engineer_video(tid)

    with SessionLocal() as db:
        tmpl = db.get(PromptTemplate, uuid.UUID(tid))
        assert tmpl.status == "failed"
        assert tmpl.transcript is None
        assert "paroles" in tmpl.error and "HTTP 401" in tmpl.error


def test_retranscribe_repare_un_template_long(client):
    # « Re-transcrire » relance la transcription EN DIRECT et rend le template
    # utilisable (paroles tagguées H/F majoritairement masculin).
    from app.db.models import PromptTemplate

    with SessionLocal() as db:
        t = PromptTemplate(
            tenant_id="tnt-rv", category="storytelling_long",
            template_text="A woman {outfit}. {characteristics}. {dialogue}",
            speaking=True, status="failed", source_video_url="https://r2.example/x.mp4")
        db.add(t)
        db.commit()
        tid = str(t.id)
    with patch("app.workers.picture_tasks._transcribe_video",
               return_value=("Hello there. How are you. I am fine.", "")), \
         patch("app.workers.tasks._download"):
        r = client.post(f"/api/banks/templates/{tid}/retranscribe")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    with SessionLocal() as db:
        t = db.get(PromptTemplate, uuid.UUID(tid))
        assert t.status == "ready"
        assert t.transcript.startswith("[H]") and t.error is None


def test_retranscribe_renvoie_l_erreur_exacte(client):
    from app.db.models import PromptTemplate

    with SessionLocal() as db:
        t = PromptTemplate(
            tenant_id="tnt-rv", category="storytelling_long",
            template_text="A woman {outfit}. {dialogue}", speaking=True,
            status="ready", source_video_url="https://r2.example/y.mp4")
        db.add(t)
        db.commit()
        tid = str(t.id)
    with patch("app.workers.picture_tasks._transcribe_video",
               return_value=("", "transcription ElevenLabs échouée : HTTP 401")), \
         patch("app.workers.tasks._download"):
        r = client.post(f"/api/banks/templates/{tid}/retranscribe")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "HTTP 401" in body["error"]
    with SessionLocal() as db:  # template long sans paroles → repassé failed
        assert db.get(PromptTemplate, uuid.UUID(tid)).status == "failed"


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
