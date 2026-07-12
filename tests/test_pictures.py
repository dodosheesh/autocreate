import random

import pytest
from PIL import Image

from app.media.scrub import strip_metadata
from app.services.composer import CharacteristicInput
from app.services.estimator import (
    MissingRateError,
    Rate,
    estimate_pictures,
    max_pictures_for_budget,
)
from app.services.picture_composer import (
    PictureComposeError,
    compose_pictures,
)
from app.services.variation import Option, outfit_option

RATES = [Rate("nano_banana", None, None, "per_image", 0.02)]
FACE = "https://r2.example/face.jpg"
CHARACS = [
    CharacteristicInput(
        label="tattoo",
        reference_image_url="https://r2.example/tattoo.jpg",
        injection_hint="a floral tattoo on her forearm",
        seedream=True,  # photo : uniquement les caractéristiques cochées seedream
    )
]


# ---------- estimateur ----------


def test_estimate_pictures():
    est = estimate_pictures(100, RATES, qc_success_rate=0.80)
    assert est.gross_usd == pytest.approx(2.0)
    assert est.effective_usd == pytest.approx(2.5)
    assert est.total_items == 100


def test_estimate_pictures_tarif_manquant():
    with pytest.raises(MissingRateError):
        estimate_pictures(10, [], qc_success_rate=0.8)


def test_max_pictures_for_budget():
    # 0.02 / 0.8 = 0.025 $ livré ; 5 $ → 200 photos
    assert max_pictures_for_budget(5.0, RATES, qc_success_rate=0.80) == 200


def test_estimate_pictures_par_resolution():
    # L'estimation s'adapte selon 1K/2K (seedream)
    rates = [
        Rate("seedream", "1K", None, "per_image", 0.035),
        Rate("seedream", "2K", None, "per_image", 0.07),
    ]
    est_1k = estimate_pictures(10, rates, model="seedream", resolution="1K", qc_success_rate=1.0)
    est_2k = estimate_pictures(10, rates, model="seedream", resolution="2K", qc_success_rate=1.0)
    assert est_1k.gross_usd == pytest.approx(0.35)
    assert est_2k.gross_usd == pytest.approx(0.70)  # 2× plus cher, sans rien changer d'autre


# ---------- composition ----------


def prompts_pool(n=3):
    return [Option(id=f"p{i}", text=f"a cinematic portrait, scene {i}") for i in range(n)]


def outfits_pool(n=2):
    return [
        outfit_option(f"o{i}", ["red dress"], f"https://r2.example/o{i}.jpg", 1.0)
        for i in range(n)
    ]


def test_compose_pictures_dedup_et_count():
    result = compose_pictures(
        6, prompts_pool(3), outfits_pool(2), CHARACS, FACE, rng=random.Random(1)
    )
    assert len(result.items) == 6
    assert result.shortfall == 0
    assert len({i.combo_hash for i in result.items}) == 6


def test_compose_pictures_applique_le_style_amateur():
    style = "Casual amateur smartphone snapshot, natural light."
    result = compose_pictures(
        1, prompts_pool(1), [], CHARACS, FACE, rng=random.Random(3), style_suffix=style,
    )
    assert result.items[0].filled_prompt.endswith(style)
    # sans style_suffix : aucun ajout
    plain = compose_pictures(1, prompts_pool(1), [], CHARACS, FACE, rng=random.Random(3))
    assert not plain.items[0].filled_prompt.endswith(style)


def test_compose_pictures_shortfall():
    # 2 prompts × 1 outfit = 2 combos pour 5 demandés
    result = compose_pictures(
        5, prompts_pool(2), outfits_pool(1), CHARACS, FACE, rng=random.Random(2)
    )
    assert len(result.items) == 2
    assert result.shortfall == 3


def test_compose_injecte_caracteristiques_et_outfit():
    result = compose_pictures(
        1, prompts_pool(1), outfits_pool(1), CHARACS, FACE, rng=random.Random(3)
    )
    item = result.items[0]
    assert "floral tattoo on her forearm" in item.filled_prompt
    assert "red dress" in item.filled_prompt
    assert item.reference_image_urls[0] == FACE
    assert "https://r2.example/tattoo.jpg" in item.reference_image_urls


def test_compose_sans_prompt_leve_erreur():
    with pytest.raises(PictureComposeError, match="Aucun prompt"):
        compose_pictures(1, [], outfits_pool(1), CHARACS, FACE)


def test_compose_refs_cap_nano_banana():
    # 20 caractéristiques cochées SEEDREAM (toutes injectées) → doit couper à max_refs.
    many = [
        CharacteristicInput(
            id=f"t{i}",
            label=f"t{i}",
            reference_image_url=f"https://r2.example/t{i}.jpg",
            injection_hint=f"trait {i}",
            priority=i,
            seedream=True,
        )
        for i in range(20)
    ]
    result = compose_pictures(
        1, prompts_pool(1), [], many, FACE, max_refs=10, rng=random.Random(4)
    )
    assert len(result.items[0].reference_image_urls) == 10
    assert result.items[0].reference_image_urls[0] == FACE


# ---------- scrub métadonnées (vrai Pillow) ----------


def _make_jpeg_with_exif(path):
    img = Image.new("RGB", (32, 32), (120, 60, 200))
    exif = img.getexif()
    exif[0x010E] = "secret description"  # ImageDescription
    exif[0x0131] = "autocreate-test"  # Software
    img.save(path, "JPEG", exif=exif)


def test_scrub_retire_exif(tmp_path):
    src = tmp_path / "in.jpg"
    out = tmp_path / "out.jpg"
    _make_jpeg_with_exif(str(src))
    # sanity : l'EXIF est bien présent à la source
    assert dict(Image.open(str(src)).getexif())
    strip_metadata(str(src), str(out), output_format="jpeg")
    cleaned = Image.open(str(out))
    assert not dict(cleaned.getexif())  # plus aucune métadonnée EXIF
    assert cleaned.size == (32, 32)  # pixels préservés


def test_scrub_png_retire_les_chunks_texte(tmp_path):
    from PIL.PngImagePlugin import PngInfo

    src = tmp_path / "in.png"
    out = tmp_path / "out.png"
    meta = PngInfo()
    meta.add_text("Comment", "generated by X")
    Image.new("RGB", (16, 16), (10, 20, 30)).save(str(src), "PNG", pnginfo=meta)
    assert Image.open(str(src)).info.get("Comment") == "generated by X"
    strip_metadata(str(src), str(out), output_format="png")
    assert "Comment" not in Image.open(str(out)).info


def test_scrub_refuse_image_trop_grande(tmp_path, monkeypatch):
    """Garde-fou decompression bomb : une image au-dessus du plafond est rejetée."""
    import app.media.scrub as scrub

    monkeypatch.setattr(scrub, "MAX_IMAGE_PIXELS", 100)  # plafond bas pour le test
    src = tmp_path / "big.png"
    Image.new("RGB", (50, 50), (1, 2, 3)).save(str(src), "PNG")  # 2500 px > 100
    with pytest.raises(scrub.ImageTooLargeError):
        scrub.strip_metadata(str(src), str(tmp_path / "out.png"), output_format="png")
