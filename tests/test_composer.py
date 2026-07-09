from app.services.composer import (
    CharacteristicInput,
    combo_hash,
    inject_characteristics,
    select_reference_images,
)


def charac(label: str, priority: int = 0, always: bool = True) -> CharacteristicInput:
    return CharacteristicInput(
        label=label,
        reference_image_url=f"https://r2.example/{label}.jpg",
        injection_hint=f"a visible {label}",
        priority=priority,
        always_include=always,
    )


def test_injection_dans_le_slot():
    prompt = "A woman at the beach, {characteristics}, golden hour."
    result = inject_characteristics(prompt, [charac("tattoo"), charac("piercing", priority=1)])
    assert "{characteristics}" not in result
    assert "a visible tattoo, a visible piercing" in result


def test_injection_sans_slot_ajoute_en_fin():
    result = inject_characteristics("A woman at the beach.", [charac("tattoo")])
    assert result == "A woman at the beach. A visible tattoo."


def test_injection_respecte_always_include_et_priorite():
    result = inject_characteristics(
        "Scene {characteristics}",
        [charac("z-trait", priority=2), charac("a-trait", priority=1), charac("off", always=False)],
    )
    assert result == "Scene a visible a-trait, a visible z-trait"


def test_selection_refs_face_toujours_premiere_et_cap_12():
    characs = [charac(f"trait{i}", priority=i) for i in range(20)]
    refs = select_reference_images("https://r2.example/face.jpg", characs, max_refs=12)
    assert refs[0] == "https://r2.example/face.jpg"
    assert len(refs) == 12
    # Priorités les plus basses d'abord, coupé à 11 traits après la face
    assert refs[1] == "https://r2.example/trait0.jpg"
    assert "https://r2.example/trait11.jpg" not in refs


def test_selection_refs_dedoublonne_et_ajoute_extras():
    refs = select_reference_images(
        "https://r2.example/face.jpg",
        [charac("tattoo")],
        extra_refs=["https://r2.example/outfit.jpg", "https://r2.example/face.jpg"],
    )
    assert refs == [
        "https://r2.example/face.jpg",
        "https://r2.example/tattoo.jpg",
        "https://r2.example/outfit.jpg",
    ]


def test_combo_hash_stable_et_sensible_au_contenu():
    a = combo_hash({"prompt": "x", "outfit": "1"})
    b = combo_hash({"outfit": "1", "prompt": "x"})  # ordre des clés indifférent
    c = combo_hash({"prompt": "x", "outfit": "2"})
    assert a == b
    assert a != c
