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


def camera_directive(category: str, speaking: bool) -> str:
    """Directive caméra/audio à coller en fin de prompt selon la catégorie."""
    if category == "snapchat":
        directive = (
            "Filmed candidly by another person holding a phone: the camera can move and "
            "zoom naturally like a real handheld recording. Realistic phone-camera look, "
            "not cinematic, not AI-looking. No music."
        )
    else:
        directive = (
            "She is filming herself on her own phone (propped up or at arm's length). "
            "STATIC locked camera: the camera stays completely still — no zoom, no pan, "
            "no push-in, no dolly, no camera movement of any kind. Natural vertical phone "
            "video for social media (TikTok/Reels), candid and realistic, not cinematic, "
            "not AI-looking. No music."
        )
    if speaking:
        directive += (
            " The spoken lines are the girl's OWN real natural voice (she simply lowers "
            "her voice when needed), authentic spoken delivery — not sung, no voice-over, "
            "no background music."
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
