"""Génération photo via Seedream Edit (multi-références)."""

import uuid
from unittest.mock import patch

from app.db.base import Base, SessionLocal, engine
from app.db.models import ItemStatus, Model, PictureItem, PictureJob
from app.integrations.kie import build_seedream_input
from app.workers import picture_tasks as pt


def test_build_seedream_input_multi_refs():
    payload = build_seedream_input(
        "a portrait", ["https://r2/face.jpg", "https://r2/tattoo.jpg"],
        image_size="9:16", resolution="2K",
    )
    assert payload == {
        "prompt": "a portrait",
        "image_urls": ["https://r2/face.jpg", "https://r2/tattoo.jpg"],
        "aspect_ratio": "9:16",
        "quality": "high",  # 2K → high (schéma seedream)
        "output_format": "png",
    }
    # 1K → basic
    assert build_seedream_input("p", ["u"], resolution="1K")["quality"] == "basic"


def test_dispatch_photo_utilise_seedream():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        m = Model(tenant_id="tnt-sd", name="M", face_reference_url="https://pub.r2.dev/f.jpg")
        db.add(m); db.flush()
        job = PictureJob(tenant_id="tnt-sd", model_id=m.id, status="dispatched",
                         count=1, image_size="9:16")
        db.add(job); db.flush()
        item = PictureItem(job_id=job.id, combo_hash="h", filled_prompt="a portrait",
                           reference_image_urls=["https://pub.r2.dev/f.jpg", "https://pub.r2.dev/t.jpg"],
                           status=ItemStatus.COMPOSED)
        db.add(item); db.commit(); item_id = str(item.id)

    with patch("app.workers.picture_tasks.kie.create_seedream_task", return_value="seedream-task-1") as mk:
        pt.dispatch_nano_banana(item_id)

    # le modèle utilisé est bien Seedream, avec les multi-références
    payload = mk.call_args[0][0]
    assert payload["image_urls"] == ["https://pub.r2.dev/f.jpg", "https://pub.r2.dev/t.jpg"]
    assert payload["aspect_ratio"] == "9:16"
    assert payload["quality"] in ("basic", "high")
    with SessionLocal() as db:
        it = db.get(PictureItem, uuid.UUID(item_id))
        assert it.status == ItemStatus.DISPATCHED
        assert it.kie_task_id == "seedream-task-1"
