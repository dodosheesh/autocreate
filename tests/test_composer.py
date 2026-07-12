import random

from app.services.composer import (
    CharacteristicInput,
    combo_hash,
    inject_characteristics,
    select_active_characteristics,
    select_photo_characteristics,
    select_reference_images,
)


def charac(label: str, priority: int = 0, recurring: bool = False, seedream: bool = False) -> CharacteristicInput:
    return CharacteristicInput(
        id=label,
        label=label,
        reference_image_url=f"https://r2.example/{label}.jpg",
        injection_hint=f"a visible {label}",
        priority=priority,
        recurring=recurring,
        seedream=seedream,
    )


def test_select_photo_seulement_les_seedream_aucun_pool():
    chars = [charac("a", seedream=True, priority=1), charac("b"), charac("c"),
             charac("d", seedream=True, priority=0)]
    photo = select_photo_characteristics(chars)
    # uniquement les cochées seedream, triées par priorité, aucun tirage du pool
    assert [c.label for c in photo] == ["d", "a"]


def test_select_photo_aucune_cochee_renvoie_vide():
    # aucune seedream → aucune caractéristique (seul le visage servira de réf.)
    assert select_photo_characteristics([charac("a"), charac("b")]) == []


def test_injection_dans_le_slot():
    prompt = "A woman at the beach, {characteristics}, golden hour."
    result = inject_characteristics(prompt, [charac("tattoo"), charac("piercing", priority=1)])
    assert "{characteristics}" not in result
    assert "a visible tattoo, a visible piercing" in result


def test_injection_sans_slot_ajoute_en_fin():
    result = inject_characteristics("A woman at the beach.", [charac("tattoo")])
    assert result == "A woman at the beach. A visible tattoo."


def test_injection_respecte_priorite():
    # inject_characteristics injecte TOUTES les caractéristiques fournies (déjà
    # sélectionnées en amont), triées par priorité.
    result = inject_characteristics(
        "Scene {characteristics}",
        [charac("z-trait", priority=2), charac("a-trait", priority=1)],
    )
    assert result == "Scene a visible a-trait, a visible z-trait"


def test_select_active_une_seule_aleatoire_plus_recurrentes():
    chars = [charac("tattoo", recurring=True), charac("a"), charac("b"), charac("c")]
    for seed in range(8):
        active = select_active_characteristics(chars, random.Random(seed))
        labels = {c.label for c in active}
        assert "tattoo" in labels  # la récurrente est toujours là
        pool_picked = labels - {"tattoo"}
        assert len(pool_picked) == 1  # exactement UNE non-récurrente
        assert pool_picked <= {"a", "b", "c"}


def test_select_active_varie_selon_le_tirage():
    chars = [charac("a"), charac("b"), charac("c"), charac("d")]
    picks = {select_active_characteristics(chars, random.Random(s))[0].label for s in range(20)}
    assert len(picks) > 1  # le tirage change bien d'un média à l'autre


def test_select_active_sans_non_recurrente():
    chars = [charac("tattoo", recurring=True)]
    active = select_active_characteristics(chars, random.Random(0))
    assert [c.label for c in active] == ["tattoo"]  # rien à tirer, juste la récurrente


def test_select_active_vide():
    assert select_active_characteristics([], random.Random(0)) == []


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
