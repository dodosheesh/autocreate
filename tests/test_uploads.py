"""Endpoint d'upload : fichier → R2 (mocké) → URL, scopé au tenant, auth requise."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.db.base import Base, SessionLocal, engine
from app.db.models import User
from app.main import app
from app.services.security import hash_password

EMAIL = "up@example.com"
PW = "upload-pass-123"


@pytest.fixture(scope="module")
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add(User(email=EMAIL, password_hash=hash_password(PW), tenant_id="tnt-up"))
        db.commit()
    c = TestClient(app)
    c.post("/api/auth/login", json={"email": EMAIL, "password": PW})
    return c


def test_upload_image_ok(client):
    captured = {}

    def fake_upload(local_path, key, content_type="video/mp4"):
        captured["key"] = key
        captured["content_type"] = content_type
        return f"https://r2.example/{key}"

    with patch("app.api.routers.uploads.r2.upload_file", side_effect=fake_upload):
        r = client.post(
            "/api/uploads",
            files={"file": ("face.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"].startswith("https://r2.example/uploads/tnt-up/")
    assert body["url"].endswith(".png")
    assert captured["content_type"] == "image/png"
    # clé scopée au tenant de l'utilisateur
    assert captured["key"].startswith("uploads/tnt-up/")


def test_upload_type_refuse(client):
    r = client.post(
        "/api/uploads", files={"file": ("x.exe", b"MZ", "application/octet-stream")}
    )
    assert r.status_code == 415


def test_upload_exige_auth():
    anon = TestClient(app)
    r = anon.post("/api/uploads", files={"file": ("a.png", b"x", "image/png")})
    assert r.status_code == 401
