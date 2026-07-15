"""Tableau de bord des coûts : agrégation par fenêtre + par mois, scopée tenant."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db.base import Base, SessionLocal, engine
from app.db.models import GenerationJob, Model, PictureJob, User
from app.main import app
from app.services.security import hash_password

EMAIL = "costs@example.com"
PW = "costs-dashboard-1"
TENANT = "tnt-costs"


def _client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        db.add(User(email=EMAIL, password_hash=hash_password(PW), tenant_id=TENANT))
        # autre tenant : ne doit PAS être compté
        db.add(User(email="other@example.com", password_hash=hash_password(PW), tenant_id="tnt-other"))
        m = Model(name="m", face_reference_url="https://r2/f.jpg", tenant_id=TENANT)
        mo = Model(name="mo", face_reference_url="https://r2/f.jpg", tenant_id="tnt-other")
        db.add_all([m, mo])
        db.flush()
        # vidéo récente (réel 2.0), vidéo vieille de 40 j (réel 5.0), photo récente (estimé 1.0)
        db.add(GenerationJob(model_id=m.id, tenant_id=TENANT, actual_cost_usd=2.0,
                             estimated_cost_usd=1.5, created_at=now - timedelta(days=1)))
        db.add(GenerationJob(model_id=m.id, tenant_id=TENANT, actual_cost_usd=5.0,
                             created_at=now - timedelta(days=40)))
        db.add(PictureJob(model_id=m.id, tenant_id=TENANT, actual_cost_usd=None,
                          estimated_cost_usd=1.0, created_at=now - timedelta(days=2)))
        # autre tenant : gros coût, jamais compté
        db.add(GenerationJob(model_id=mo.id, tenant_id="tnt-other", actual_cost_usd=999.0,
                             created_at=now - timedelta(days=1)))
        db.commit()
    c = TestClient(app)
    c.post("/api/auth/login", json={"email": EMAIL, "password": PW})
    return c


def test_cost_summary_fenetres_et_scope():
    c = _client()
    data = c.get("/api/costs/summary").json()
    wins = {w["key"]: w for w in data["windows"]}

    # 7 jours : vidéo récente (2.0 réel) + photo récente (1.0 estimé) = 3.0 ; la
    # vidéo de 40 j n'y est pas ; le tenant voisin (999) jamais compté.
    assert wins["7d"]["video_usd"] == 2.0
    assert wins["7d"]["photo_usd"] == 1.0
    assert wins["7d"]["total_usd"] == 3.0
    # Total : inclut la vidéo de 40 j → 2.0 + 5.0 + 1.0 = 8.0
    assert wins["all"]["total_usd"] == 8.0
    assert wins["all"]["video_jobs"] == 2 and wins["all"]["photo_jobs"] == 1
    # 30 jours : exclut la vidéo de 40 j
    assert wins["30d"]["total_usd"] == 3.0


def test_cost_summary_par_mois():
    c = _client()
    data = c.get("/api/costs/summary").json()
    total = sum(m["total_usd"] for m in data["by_month"])
    assert round(total, 2) == 8.0  # même total ventilé par mois


def test_cost_summary_exige_auth():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    assert TestClient(app).get("/api/costs/summary").status_code == 401
