"""Templates par défaut (bibliothèque) + éditeur de tarifs."""

import pytest
from fastapi.testclient import TestClient

from app.db.base import Base, SessionLocal, engine
from app.db.init_db import SEED_PRICING
from app.db.models import Pricing, User
from app.main import app
from app.services.security import hash_password
from app.services.template_library import DEFAULT_TEMPLATES

EMAIL = "set@example.com"
PW = "settings-pass-1"


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
        db.add(User(email=EMAIL, password_hash=hash_password(PW), tenant_id="tnt-set"))
        db.commit()
    c = TestClient(app)
    c.post("/api/auth/login", json={"email": EMAIL, "password": PW})
    return c


def test_load_default_templates_idempotent(client):
    r = client.post("/api/banks/templates/load-defaults")
    assert r.status_code == 200
    assert r.json()["added"] == len(DEFAULT_TEMPLATES)
    # tous chargés et visibles
    templates = client.get("/api/banks/templates").json()
    assert len(templates) == len(DEFAULT_TEMPLATES)
    # 2e appel : rien de dupliqué
    r2 = client.post("/api/banks/templates/load-defaults")
    assert r2.json()["added"] == 0
    assert len(client.get("/api/banks/templates").json()) == len(DEFAULT_TEMPLATES)


def test_default_templates_slots_et_speaking_coherents():
    for category, text, speaking in DEFAULT_TEMPLATES:
        assert "{outfit}" in text and "{background}" in text
        # un template speaking doit exposer le slot dialogue
        if speaking:
            assert "{dialogue}" in text
        # snapchat doit exposer le slot caption
        if category == "snapchat":
            assert "{caption}" in text


def test_pricing_list_et_edition(client):
    rows = client.get("/api/pricing").json()
    assert rows
    s2s = next(r for r in rows if r["model"] == "elevenlabs_s2s")
    assert s2s["rate_usd"] == pytest.approx(0.12)  # valeur corrigée

    # éditer un tarif se reflète dans l'estimation
    seed720 = next(r for r in rows if r["model"] == "seedance_2.0" and r["resolution"] == "720p")
    client.put(f"/api/pricing/{seed720['id']}", json={"rate_usd": 0.20})
    est = client.post(
        "/api/estimate", json={"count": 1, "duration_s": 10, "resolution": "720p"}
    ).json()
    assert est["gross_usd"] == pytest.approx(2.0)  # 10s × 0.20


def test_pricing_upsert(client):
    r = client.post(
        "/api/pricing",
        json={"model": "seedance_2.5", "resolution": "720p", "with_ref": True,
              "unit": "per_sec", "rate_usd": 0.15},
    )
    assert r.status_code == 200
    # upsert : re-poster met à jour au lieu de dupliquer
    r2 = client.post(
        "/api/pricing",
        json={"model": "seedance_2.5", "resolution": "720p", "with_ref": True,
              "unit": "per_sec", "rate_usd": 0.18},
    )
    assert r2.json()["id"] == r.json()["id"]
    assert r2.json()["rate_usd"] == pytest.approx(0.18)
