import random

import pytest

from app.services.composer import CharacteristicInput
from app.services.variation import (
    CategoryPools,
    ComposeError,
    Option,
    TemplateOption,
    background_option,
    compose_batch,
    fill_template,
    outfit_option,
    weighted_draw,
)

FACE = "https://r2.example/face.jpg"

CHARACS = [
    CharacteristicInput(
        label="tattoo",
        reference_image_url="https://r2.example/tattoo.jpg",
        injection_hint="a floral tattoo on her forearm",
    )
]


def pools(
    n_templates=2, n_outfits=3, n_backgrounds=3, speaking=False, with_caption=False, n_dialogues=2
) -> CategoryPools:
    caption_part = " Caption: {caption}" if with_caption else ""
    dialogue_part = " {dialogue}" if speaking else ""
    return CategoryPools(
        templates=[
            TemplateOption(
                id=f"t{i}",
                template_text=(
                    f"Scene {i}: {{outfit}}, {{background}}, "
                    f"{{characteristics}}.{dialogue_part}{caption_part}"
                ),
                speaking=speaking,
            )
            for i in range(n_templates)
        ],
        outfits=[
            outfit_option(f"o{i}", ["red dress"], f"https://r2.example/o{i}.jpg", 1.0)
            for i in range(n_outfits)
        ],
        backgrounds=[
            background_option(f"b{i}", ["beach at sunset"], f"https://r2.example/b{i}.jpg", 1.0)
            for i in range(n_backgrounds)
        ],
        dialogues=[
            Option(id=f"d{i}", text=f"[H] line {i}\n[F] answer {i}") for i in range(n_dialogues)
        ],
        captions=[Option(id="c0", text="user caption")],
    )


def test_compose_batch_dedup_et_count():
    result = compose_batch(
        {"skit": 10}, {"skit": pools()}, CHARACS, FACE, rng=random.Random(42)
    )
    assert len(result.items) == 10
    assert result.shortfall_per_category == {}
    hashes = [i.combo_hash for i in result.items]
    assert len(set(hashes)) == 10  # zéro doublon


def test_shortfall_quand_combos_epuises():
    # 1 template × 2 outfits × 1 background = 2 combos possibles pour 5 demandés
    small = pools(n_templates=1, n_outfits=2, n_backgrounds=1)
    result = compose_batch(
        {"skit": 5}, {"skit": small}, CHARACS, FACE, rng=random.Random(1)
    )
    assert len(result.items) == 2
    assert result.shortfall_per_category == {"skit": 3}


def test_slots_remplis_et_caracteristiques_injectees():
    result = compose_batch(
        {"skit": 1}, {"skit": pools()}, CHARACS, FACE, rng=random.Random(7)
    )
    prompt = result.items[0].filled_prompt
    assert "{" not in prompt  # tous les slots résolus
    assert "wearing red dress" in prompt
    assert "beach at sunset" in prompt
    assert "a floral tattoo on her forearm" in prompt


def test_refs_incluent_face_caracteristiques_outfit_background():
    result = compose_batch(
        {"skit": 1},
        {"skit": pools(n_outfits=1, n_backgrounds=1)},
        CHARACS,
        FACE,
        rng=random.Random(3),
    )
    refs = result.items[0].reference_image_urls
    assert refs[0] == FACE
    assert "https://r2.example/tattoo.jpg" in refs
    assert "https://r2.example/o0.jpg" in refs
    assert "https://r2.example/b0.jpg" in refs


def test_categorie_parlante_rend_le_dialogue_et_garde_le_script():
    result = compose_batch(
        {"podcast": 1}, {"podcast": pools(speaking=True)}, CHARACS, FACE, rng=random.Random(5)
    )
    item = result.items[0]
    assert item.speaking is True
    assert item.dialogue_script.startswith("[H]")
    assert "deep masculine voice" in item.filled_prompt  # rendu naturel dans le prompt


def test_speaking_sans_banque_dialogues_leve_erreur():
    empty = pools(speaking=True, n_dialogues=0)
    with pytest.raises(ComposeError, match="dialogues vide"):
        compose_batch({"podcast": 1}, {"podcast": empty}, CHARACS, FACE, rng=random.Random(5))


def test_slot_caption_rempli_depuis_la_banque():
    result = compose_batch(
        {"snapchat": 1},
        {"snapchat": pools(with_caption=True)},
        CHARACS,
        FACE,
        rng=random.Random(9),
    )
    item = result.items[0]
    assert item.caption_text == "user caption"
    assert "Caption: user caption" in item.filled_prompt


def test_categorie_sans_template_leve_erreur():
    with pytest.raises(ComposeError, match="aucun template"):
        compose_batch({"skit": 1}, {}, CHARACS, FACE)


def test_weighted_draw_respecte_les_poids():
    options = [Option(id="rare", weight=0.0), Option(id="common", weight=10.0)]
    rng = random.Random(0)
    draws = {weighted_draw(options, rng).id for _ in range(50)}
    assert draws == {"common"}


def test_fill_template_laisse_les_slots_inconnus():
    assert fill_template("{outfit} and {mystery}", {"outfit": "x"}) == "x and {mystery}"
