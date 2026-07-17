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
STATIC_CATEGORIES = {
    "skit", "storytelling", "storytelling_long", "showing_body", "micro_trottoir", "podcast",
}


# Continuité : garde le MÊME outfit et une seule prise du début à la fin. Empêche
# Seedance de « fondre » deux tenues (photo visage vs référence outfit) ou de couper.
_CONTINUITY = (
    "Single continuous take, one shot from start to finish — no cut, no jump cut, no "
    "scene change. She keeps the EXACT SAME outfit for the entire clip: no outfit change, "
    "no wardrobe swap, no morphing between looks, the clothing stays identical throughout."
)

# L'appareil qui filme ne doit JAMAIS apparaître (c'est le point de vue caméra).
_NO_DEVICE = (
    "The recording device (phone or camera) is NOT visible anywhere in the frame — it is "
    "the camera's own point of view, never shown: no phone, no tripod, no phone-on-a-stand "
    "on screen, and no mirror reflection (unless the scene is clearly an indoor room)."
)

# ANTI-STATIQUE : elle FAIT toujours quelque chose, jamais figée comme un poteau.
# La bulle de chewing-gum est réservée au showing_body (outfit check, non parlé) :
# partout ailleurs elle parle face caméra et la bulle rendait mal.
def _liveliness(category: str) -> str:
    gum = ", blowing a bubble with gum" if category == "showing_body" else ""
    return (
        "She is ALWAYS doing something natural, cute and lively and NEVER just stands still "
        "like a statue or a pole: continuous subtle movement and playful gestures — shifting "
        "her weight, a little hip sway, a hand on her hip, tilting her head, playing with her "
        f"hair, a cute playful expression, lifting a leg slightly{gum}, a "
        "small pose change. The clip must feel alive and engaging for social media, never frozen."
    )


def camera_directive(category: str, speaking: bool) -> str:
    """Directive caméra/audio à coller en fin de prompt selon la catégorie."""
    if category == "snapchat":
        # Filmé par un tiers : la caméra peut bouger. Pas de clause « elle se filme ».
        directive = (
            "Filmed candidly by another person holding a phone: the camera can move and "
            "zoom naturally like a real handheld recording. Realistic phone-camera look, "
            "not cinematic, not AI-looking. No music. "
            "Render ABSOLUTELY NO text, letters, words, captions, subtitles, speech "
            "bubbles, stickers or UI overlays anywhere in the frame — the video must be "
            f"completely text-free (the caption is added afterward in post-production). {_CONTINUITY}"
        )
        if speaking:
            directive += _SPEAKING_CLAUSE
        return directive

    if category == "podcast":
        # VRAI podcast : caméra fixe sur trépied (PAS un téléphone, PAS un selfie).
        cam = (
            "Real podcast clip filmed on a proper camera on a tripod — this is NOT a phone "
            "and NOT a selfie: she sits at a podcast microphone and talks like a guest on a "
            "podcast, filmed from a fixed third-person camera angle. "
            "STATIC locked camera: no zoom, no pan, no movement. Realistic, not cinematic, "
            "not AI-looking. No music."
        )
    else:  # skit, storytelling, showing_body, micro_trottoir → elle se filme au téléphone
        cam = (
            "She films herself on her OWN phone, which IS the camera (the phone's own point "
            "of view). STATIC locked camera: the camera stays completely still — no zoom, no "
            "pan, no push-in, no dolly, no camera movement of any kind. Natural vertical phone "
            "video for social media (TikTok/Reels), candid and realistic, not cinematic, not "
            "AI-looking. No music."
        )
    directive = f"{cam} {_NO_DEVICE} {_liveliness(category)} {_CONTINUITY}"
    if speaking:
        directive += _SPEAKING_CLAUSE
    return directive


_SPEAKING_CLAUSE = (
    " The spoken lines are the girl's OWN real natural voice (she simply lowers her voice "
    "when needed). She speaks at a natural, calm, clearly understandable pace — NEVER "
    "rushed or sped up. She delivers the lines back-to-back with NO long pause and NO "
    "silence or dead air between them. Not sung, no voice-over, no background music."
)


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
