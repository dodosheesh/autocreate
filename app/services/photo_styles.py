"""Styles photo sélectionnables à la génération (cases à cocher UI).

Chaque style ajoute un modificateur en fin de prompt pour orienter le rendu.
On les combine simplement s'il y en a plusieurs de cochés."""

# clé machine → (libellé UI, modificateur ajouté au prompt)
PHOTO_STYLES: dict[str, tuple[str, str]] = {
    "facecam_selfie": (
        "Facecam / selfie",
        "Front-facing selfie taken with the phone at arm's length, looking straight "
        "into the camera, casual facecam framing.",
    ),
    "amateur": (
        "Amateur",
        "Casual amateur smartphone snapshot, natural available light, candid and "
        "unposed, authentic everyday look, no professional retouching, no studio "
        "lighting, not a glossy magazine photo.",
    ),
    "professional": (
        "Professionnel",
        "Professional photograph, clean studio-quality lighting, sharp focus, "
        "polished and high quality.",
    ),
    "amateur_blurry": (
        "Amateur flou",
        "Casual amateur phone snapshot that is slightly blurry and soft, with motion "
        "blur, low-light grain and an imperfect out-of-focus candid feel.",
    ),
}


def build_style_suffix(keys: list[str] | None) -> str:
    """Concatène les modificateurs des styles cochés (ordre du catalogue)."""
    selected = set(keys or [])
    return " ".join(
        modifier for key, (_label, modifier) in PHOTO_STYLES.items() if key in selected
    )
