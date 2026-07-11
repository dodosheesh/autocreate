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
