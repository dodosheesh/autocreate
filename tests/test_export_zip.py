"""Export ZIP des médias approuvés (photos/vidéos) — médias seuls, sans JSON."""

import io
import uuid
import zipfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.db.base import Base, SessionLocal, engine
from app.db.models import ItemStatus, Model, PictureItem, PictureJob, ReviewStatus, User
from app.main import app
from app.services.security import hash_password

EMAIL = "zip@example.com"
PW = "zip-pass-1"


@pytest.fixture(scope="module")
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add(User(email=EMAIL, password_hash=hash_password(PW), tenant_id="tnt-zip"))
        db.commit()
    c = TestClient(app)
    c.post("/api/auth/login", json={"email": EMAIL, "password": PW})
    return c


def _job_with_approved(tenant="tnt-zip", n=2):
    with SessionLocal() as db:
        m = Model(tenant_id=tenant, name="M", face_reference_url="https://pub.r2.dev/f.jpg")
        db.add(m); db.flush()
        job = PictureJob(tenant_id=tenant, model_id=m.id, status="completed", count=n)
        db.add(job); db.flush()
        for i in range(n):
            db.add(PictureItem(
                job_id=job.id, combo_hash=f"h{i}", filled_prompt=f"prompt {i}",
                status=ItemStatus.DONE, review_status=ReviewStatus.APPROVED,
                final_image_url=f"https://pub.r2.dev/pictures/{i}.png"))
        db.commit()
        return str(job.id)


def _fake_download(url, dest, **kw):
    dest.write_bytes(b"\x89PNG\r\n fake image bytes")


def test_export_zip_contient_medias_seuls(client):
    job_id = _job_with_approved(n=2)
    with patch("app.services.export_zip.safe_download", side_effect=_fake_download):
        r = client.get(f"/api/pictures/jobs/{job_id}/export.zip")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert not any(n.endswith(".json") for n in names)  # AUCUN JSON dans le zip
    assert sum(1 for n in names if n.endswith(".png")) == 2  # les 2 médias
    assert len(names) == 2  # rien d'autre que les médias


def test_export_zip_sans_approuve_409(client):
    with SessionLocal() as db:
        m = Model(tenant_id="tnt-zip", name="Empty", face_reference_url="https://pub.r2.dev/f.jpg")
        db.add(m); db.flush()
        job = PictureJob(tenant_id="tnt-zip", model_id=m.id, status="completed", count=1)
        db.add(job); db.commit(); jid = str(job.id)
    assert client.get(f"/api/pictures/jobs/{jid}/export.zip").status_code == 409


def test_export_zip_media_hs_ignore(client):
    job_id = _job_with_approved(n=1)

    def boom(url, dest, **kw):
        raise RuntimeError("download refusé")

    with patch("app.services.export_zip.safe_download", side_effect=boom):
        r = client.get(f"/api/pictures/jobs/{job_id}/export.zip")
    assert r.status_code == 200  # le zip se construit quand même
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert zf.namelist() == []  # média HS ignoré, aucun fichier ajouté


def test_export_zip_cross_tenant_404(client):
    job_id = _job_with_approved(tenant="autre", n=1)
    assert client.get(f"/api/pictures/jobs/{job_id}/export.zip").status_code == 404
