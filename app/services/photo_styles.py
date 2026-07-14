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
        "lighting, not a glossy magazine photo. Everything is in sharp focus with a "
        "deep depth of field — the background stays sharp and detailed like a real "
        "phone photo: NO background blur, NO bokeh, NO shallow depth of field.",
    ),
    "professional": (
        "Professionnel",
        "Professional photograph, clean studio-quality lighting, sharp focus, "
        "polished and high quality.",
    ),
    "amateur_blurry": (
        "Amateur flou",
        "Casual amateur phone snapshot with an overall soft, slightly grainy low-light "
        "feel and a little motion blur, imperfect and candid — but the background stays "
        "in focus (NO bokeh, NO shallow depth of field, no blurred-out background).",
    ),
}


def build_style_suffix(keys: list[str] | None) -> str:
    """Concatène les modificateurs des styles cochés (ordre du catalogue)."""
    selected = set(keys or [])
    return " ".join(
        modifier for key, (_label, modifier) in PHOTO_STYLES.items() if key in selected
    )
