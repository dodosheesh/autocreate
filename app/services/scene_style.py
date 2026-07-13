"""Directives de mise en scène ajoutées AUTOMATIQUEMENT à chaque prompt vidéo.

But : garantir un rendu « réseaux sociaux » réaliste et non « IA-obvious »,
quel que soit le template (y compris ceux reverse-engineerés qui décriraient un
mouvement de caméra).

Règle caméra demandée :
- skit / storytelling / showing_body / micro_trottoir / podcast :
  caméra FIXE, aucun zoom, aucun mouvement (elle se filme elle-même au téléphone).
- snapchat : la caméra PEUT bouger/zoomer (vidéo filmée par un tiers).
"""

# Catégories où la caméra ne bouge JAMAIS.
STATIC_CATEGORIES = {"skit", "storytelling", "showing_body", "micro_trottoir", "podcast"}


# Continuité : garde le MÊME outfit et une seule prise du début à la fin. Empêche
# Seedance de « fondre » deux tenues (photo visage vs référence outfit) ou de couper.
_CONTINUITY = (
    "Single continuous take, one shot from start to finish — no cut, no jump cut, no "
    "scene change. She keeps the EXACT SAME outfit for the entire clip: no outfit change, "
    "no wardrobe swap, no morphing between looks, the clothing stays identical throughout."
)


def camera_directive(category: str, speaking: bool) -> str:
    """Directive caméra/audio à coller en fin de prompt selon la catégorie."""
    if category == "snapchat":
        cam = (
            "Filmed candidly by another person holding a phone: the camera can move and "
            "zoom naturally like a real handheld recording. Realistic phone-camera look, "
            "not cinematic, not AI-looking. No music."
        )
    else:
        cam = (
            "She is filming herself on her own phone that is PROPPED UP on a stand or "
            "surface in front of her, filming her DIRECTLY (this is NOT a mirror selfie "
            "and there is NO mirror — do not show a mirror or a phone held up as a "
            "reflection unless the location is clearly an indoor room). "
            "STATIC locked camera: the camera stays completely still — no zoom, no pan, "
            "no push-in, no dolly, no camera movement of any kind. Natural vertical phone "
            "video for social media (TikTok/Reels), candid and realistic, not cinematic, "
            "not AI-looking. No music."
        )
    directive = f"{cam} {_CONTINUITY}"
    if speaking:
        directive += (
            " The spoken lines are the girl's OWN real natural voice (she simply lowers "
            "her voice when needed). She delivers the lines back-to-back with NO long pause "
            "and NO silence or dead air between them — the lines flow together immediately "
            "and naturally. Not sung, no voice-over, no background music."
        )
    return directive


def apply_scene_style(
    prompt: str, category: str, speaking: bool, custom_prompt: str = ""
) -> str:
    """Ajoute la demande custom (one-shot) puis la directive caméra en fin de prompt."""
    text = prompt.rstrip()
    custom = (custom_prompt or "").strip()
    if custom:
        sep = " " if text.endswith((".", "!", "?")) else ". "
        text = f"{text}{sep}{custom}"
    directive = camera_directive(category, speaking)
    sep = " " if text.endswith((".", "!", "?")) else ". "
    return f"{text}{sep}{directive}"
