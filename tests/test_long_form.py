"""Format long 30 s : composition (2 clips, split des paroles), extraction de la
dernière frame, et orchestration synchrone generate_long_form_item."""

import random
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.media.frames import build_last_frame_command
from app.services import variation
from app.services.variation import (
    CategoryPools,
    Option,
    TemplateOption,
    compose_batch,
)

TRANSCRIPT = "[F] first sentence here. second sentence here. third one. fourth one now."


def _long_pools():
    """Un template storytelling_long AVEC transcript + 1 outfit. Pas de banque
    background/dialogue/caption (le format long ne les utilise pas)."""
    tmpl = TemplateOption(
        id="t1",
        template_text="A woman {outfit} in a cozy bedroom talks to camera. {characteristics}. {dialogue}",
        speaking=True,
        transcript=TRANSCRIPT,
    )
    outfit = Option(id="o1", weight=1.0, text="wearing a red dress", image_url="https://r2/o1.jpg")
    return {
        variation.LONG_FORM_CATEGORY: CategoryPools(
            templates=[tmpl], outfits=[outfit], backgrounds=[], dialogues=[], captions=[]
        )
    }


def _compose_one_long():
    result = compose_batch(
        counts_per_category={variation.LONG_FORM_CATEGORY: 1},
        pools_by_category=_long_pools(),
        characteristics=[],
        face_reference_url="https://r2/face.jpg",
        rng=random.Random(0),
    )
    assert not result.shortfall_per_category
    assert len(result.items) == 1
    return result.items[0]


def test_long_form_compose_deux_prompts_et_deux_moities():
    item = _compose_one_long()
    # deux prompts distincts (clip 1 / clip 2), tous deux remplis
    assert item.filled_prompt and item.filled_prompt_2
    assert item.filled_prompt != item.filled_prompt_2
    # les paroles sont bien coupées en deux, moitié/moitié, sous le tag [F]
    assert item.dialogue_script.startswith("[F]")
    assert item.dialogue_script_2.startswith("[F]")
    combined = (item.dialogue_script + " " + item.dialogue_script_2)
    assert "first sentence here" in item.dialogue_script
    assert "fourth one now" in item.dialogue_script_2
    # rien n'est perdu entre les deux moitiés
    assert "third one" in combined


def test_long_form_ne_pioche_ni_background_ni_dialogue_bank():
    item = _compose_one_long()
    assert item.background_id is None  # décor baked, jamais tiré de la banque
    assert item.dialogue_id is None    # paroles du transcript, pas de la banque
    assert item.caption_id is None
    # l'outfit (et le visage) restent des références
    assert "https://r2/face.jpg" in item.reference_image_urls
    assert "https://r2/o1.jpg" in item.reference_image_urls


def test_long_form_meme_scene_meme_outfit_sur_les_deux_clips():
    item = _compose_one_long()
    # même décor + même tenue → la seule différence entre les 2 prompts est la parole
    assert "red dress" in item.filled_prompt and "red dress" in item.filled_prompt_2
    assert "cozy bedroom" in item.filled_prompt and "cozy bedroom" in item.filled_prompt_2


def test_long_form_sans_transcript_reporte_en_shortfall():
    # un template SANS transcript n'est pas éligible au format long
    tmpl = TemplateOption(id="t0", template_text="{outfit} {dialogue}", speaking=True, transcript=None)
    pools = {
        variation.LONG_FORM_CATEGORY: CategoryPools(
            templates=[tmpl], outfits=[Option(id="o", text="x")],
            backgrounds=[], dialogues=[], captions=[]
        )
    }
    result = compose_batch(
        counts_per_category={variation.LONG_FORM_CATEGORY: 1},
        pools_by_category=pools,
        characteristics=[],
        face_reference_url="https://r2/face.jpg",
        rng=random.Random(0),
    )
    assert result.items == []
    assert result.shortfall_per_category.get(variation.LONG_FORM_CATEGORY) == 1


def test_last_frame_command():
    cmd = build_last_frame_command("clip.mp4", "frame.jpg")
    joined = " ".join(cmd)
    assert "-sseof" in joined  # se place juste avant la fin
    assert "-i clip.mp4" in joined
    assert "-frames:v 1" in joined
    assert cmd[-1] == "frame.jpg"


# ---------------- orchestration synchrone (tout mocké) ----------------


def test_generate_long_form_item_enchaine_deux_clips(monkeypatch):
    """clip1 → dernière frame → clip2 (depuis la frame) → concat → assemble → R2."""
    from app.db.base import Base, SessionLocal, engine
    from app.db.models import GenerationJob, JobItem, Model
    from app.workers import tasks

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        model = Model(name="m", face_reference_url="https://r2/face.jpg", tenant_id="t")
        db.add(model)
        db.flush()
        job = GenerationJob(model_id=model.id, tenant_id="t", resolution="720p",
                            aspect="9:16", bitrate="standard",
                            counts_per_category={variation.LONG_FORM_CATEGORY: 1})
        db.add(job)
        db.flush()
        item = JobItem(
            job_id=job.id, category=variation.LONG_FORM_CATEGORY,
            combo_hash="h1", filled_prompt="PROMPT ONE",
            filled_prompt_2="PROMPT TWO", dialogue_script="[F] a",
            dialogue_script_2="[F] b", reference_image_urls=["https://r2/face.jpg"],
            status="composed",
        )
        db.add(item)
        db.commit()
        item_id = str(item.id)

    success = MagicMock(state="success", result_urls=["https://kie/out.mp4"], fail_msg=None, cost_usd=0.5)
    created_payloads = []

    def fake_create(payload, *a, **k):
        created_payloads.append(payload)
        return f"task-{len(created_payloads)}"

    with patch.object(tasks.kie, "create_seedance_task", side_effect=fake_create), \
         patch.object(tasks.kie, "get_task", return_value=success), \
         patch.object(tasks, "_download"), \
         patch.object(tasks, "extract_last_frame", return_value="frame.jpg"), \
         patch.object(tasks.r2, "upload_file",
                      side_effect=["https://r2/frame.jpg", "https://r2/final.mp4"]), \
         patch.object(tasks, "concat_clips") as concat, \
         patch.object(tasks, "assemble") as assemble_mock, \
         patch.object(tasks, "_maybe_swap_long", side_effect=lambda p, *a, **k: p):
        tasks.generate_long_form_item(item_id)

    # deux générations Seedance, avec les 2 prompts distincts
    assert len(created_payloads) == 2
    assert created_payloads[0]["prompt"] == "PROMPT ONE"
    assert created_payloads[1]["prompt"] == "PROMPT TWO"
    # le clip 2 démarre depuis la dernière frame (référence prioritaire)
    assert created_payloads[1]["reference_image_urls"][0] == "https://r2/frame.jpg"
    # 15 s par clip
    assert created_payloads[0]["duration"] == "15"
    # concat des 2 clips + assemblage exécutés
    assert concat.call_count == 1
    assert assemble_mock.call_count == 1

    with SessionLocal() as db:
        done = db.get(JobItem, __import__("uuid").UUID(item_id))
        assert done.status == "done"
        assert done.final_video_url == "https://r2/final.mp4"
        assert done.item_actual_cost == 1.0  # 0.5 + 0.5 (2 clips)
