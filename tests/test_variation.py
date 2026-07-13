import random

from app.services.composer import CharacteristicInput
from app.services.variation import (
    CategoryPools,
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


def test_caption_optionnelle_banque_vide():
    # snapchat avec slot {caption} mais AUCUNE caption en banque : compose quand même.
    p = pools(with_caption=True)
    p_no_cap = CategoryPools(templates=p.templates, outfits=p.outfits,
                             backgrounds=p.backgrounds, dialogues=p.dialogues, captions=[])
    result = compose_batch({"snapchat": 1}, {"snapchat": p_no_cap}, CHARACS, FACE,
                           rng=random.Random(5))
    assert len(result.items) == 1
    assert result.shortfall_per_category == {}


def test_camera_fixe_hors_snapchat():
    result = compose_batch({"skit": 1}, {"skit": pools()}, CHARACS, FACE, rng=random.Random(7))
    prompt = result.items[0].filled_prompt
    assert "STATIC locked camera" in prompt and "no zoom" in prompt
    assert "No music" in prompt
    # continuité : une seule prise, même outfit, pas de cut
    assert "Single continuous take" in prompt and "SAME outfit" in prompt


def test_dialogue_sans_silence_entre_les_lignes():
    result = compose_batch({"skit": 1}, {"skit": pools(speaking=True)}, CHARACS, FACE,
                           rng=random.Random(7))
    prompt = result.items[0].filled_prompt
    assert "NO long pause" in prompt and "NO silence" in prompt


def test_snapchat_autorise_mouvement_camera():
    result = compose_batch({"snapchat": 1}, {"snapchat": pools()}, CHARACS, FACE, rng=random.Random(7))
    prompt = result.items[0].filled_prompt
    assert "camera can move and zoom" in prompt
    assert "STATIC locked camera" not in prompt


def test_custom_prompt_injecte_dans_chaque_item():
    result = compose_batch({"skit": 2}, {"skit": pools()}, CHARACS, FACE,
                           rng=random.Random(3), custom_prompt="ELLE COMPTE JUSQU'A 3")
    assert all("ELLE COMPTE JUSQU'A 3" in it.filled_prompt for it in result.items)


def test_omit_background_vide_le_slot():
    result = compose_batch({"skit": 1}, {"skit": pools()}, CHARACS, FACE,
                           rng=random.Random(3), omit_background=True)
    item = result.items[0]
    assert "beach at sunset" not in item.filled_prompt  # aucun décor mentionné
    assert item.background_id is None
    assert not any("/b" in u for u in item.reference_image_urls)  # pas de ref background


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


def test_speaking_sans_banque_dialogues_compose_quand_meme():
    # Template parlant mais aucune banque de dialogues : la vidéo se compose
    # quand même (slot {dialogue} vide), sans blocage ni shortfall.
    empty = pools(speaking=True, n_dialogues=0)
    result = compose_batch(
        {"podcast": 1}, {"podcast": empty}, CHARACS, FACE, rng=random.Random(5)
    )
    assert len(result.items) == 1
    assert result.shortfall_per_category == {}
    assert result.items[0].dialogue_script is None


def test_caption_pour_overlay_pas_dans_le_prompt():
    # Le caption sert au bandeau FFmpeg (caption_text) mais n'est JAMAIS injecté
    # dans le prompt Seedance (sinon l'IA le rend en dur, mal).
    result = compose_batch(
        {"snapchat": 1},
        {"snapchat": pools(with_caption=True)},
        CHARACS,
        FACE,
        rng=random.Random(9),
    )
    item = result.items[0]
    assert item.caption_text == "user caption"       # dispo pour l'overlay FFmpeg
    assert "user caption" not in item.filled_prompt   # PAS dans le prompt Seedance


def test_template_sans_slot_background_ne_tire_aucun_decor():
    # Un template dont la scène est écrite (pas de {background}) ne doit pas
    # piocher un décor aléatoire ni sa référence image.
    p = pools()
    scene = CategoryPools(
        templates=[TemplateOption(id="sc", template_text="A woman {outfit} waits in line at a fast-food counter. {characteristics}", speaking=False)],
        outfits=p.outfits, backgrounds=p.backgrounds, dialogues=p.dialogues, captions=p.captions,
    )
    result = compose_batch({"snapchat": 1}, {"snapchat": scene}, CHARACS, FACE, rng=random.Random(4))
    item = result.items[0]
    assert item.background_id is None
    assert not any("/b" in u for u in item.reference_image_urls)  # aucune ref décor


def test_snapchat_tire_un_caption_sans_slot():
    # Même un template snapchat SANS {caption} tire un caption (pour l'overlay).
    p = pools(with_caption=True)
    no_slot = CategoryPools(
        templates=[TemplateOption(id="s0", template_text="A woman {outfit} in {background}. {characteristics}", speaking=False)],
        outfits=p.outfits, backgrounds=p.backgrounds, dialogues=p.dialogues, captions=p.captions,
    )
    result = compose_batch({"snapchat": 1}, {"snapchat": no_slot}, CHARACS, FACE, rng=random.Random(2))
    assert result.items[0].caption_text == "user caption"


def test_categorie_sans_template_reportee_en_shortfall():
    # Un style sans template ne fait PAS échouer tout le batch : il est ignoré
    # et reporté en shortfall, pendant que les styles pourvus sont composés.
    result = compose_batch(
        {"skit": 1, "podcast": 2},
        {"podcast": pools()},  # skit absent → sans template
        CHARACS,
        FACE,
        rng=random.Random(1),
    )
    assert result.shortfall_per_category == {"skit": 1}
    assert len(result.items) == 2
    assert all(it.category == "podcast" for it in result.items)


def test_weighted_draw_respecte_les_poids():
    options = [Option(id="rare", weight=0.0), Option(id="common", weight=10.0)]
    rng = random.Random(0)
    draws = {weighted_draw(options, rng).id for _ in range(50)}
    assert draws == {"common"}


def test_fill_template_laisse_les_slots_inconnus():
    assert fill_template("{outfit} and {mystery}", {"outfit": "x"}) == "x and {mystery}"
