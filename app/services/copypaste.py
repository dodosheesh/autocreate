"""Feature copypaste (vidéo → vidéo) — logique PURE.

Seedance 2 accepte des vidéos de référence : on lui envoie la vidéo source +
la photo visage de la model avec un prompt fixe de remplacement (+ demande
custom optionnelle). Pas de banque de templates ni de backgrounds : la scène,
l'action et le décor sont ceux de la vidéo de référence. En option
(add_random_assets, coché par défaut), chaque vidéo reçoit les
caractéristiques de la model (récurrentes + 1 aléatoire) et un outfit tiré
de la banque.
"""

import random

from app.services.variation import Option, weighted_draw

# Prompt FIXE de la feature : la fille de la vidéo est remplacée par la model
# (la photo visage part en référence image à côté de la vidéo).
HARD_PROMPT = "Replace the girl in the video for the girl in the picture."

# Limite Seedance 2 : la vidéo de référence ne peut pas dépasser 15 s
# (« The total duration of the video cannot exceed 15 seconds »).
MAX_REF_VIDEO_S = 15.0

# Kie/Seedance refuse parfois une référence parce que sa piste audio entre
# dans son filtre de sûreté.  Cette liste reste volontairement stricte : un
# simple mot « audio » ne doit jamais proposer de modifier une vidéo.
_AUDIO_SAFETY_MARKERS = (
    "sensitive", "safety", "policy", "moderation", "copyright", "copyright restrictions",
)


def is_audio_safety_rejection(error: str | None) -> bool:
    """True seulement pour les refus explicites liés au filtre audio.

    Utilisé comme garde-fou côté API avant de retirer une piste son : ce
    n'est pas une action de réparation générique pour les autres erreurs.
    """
    text = (error or "").casefold()
    return "audio" in text and any(marker in text for marker in _AUDIO_SAFETY_MARKERS)


def is_video_pixel_limit_rejection(error: str | None) -> bool:
    """True pour le refus Kie/Seedance de taille (nombre de pixels) vidéo."""
    text = (error or "").casefold()
    return "video pixel count" in text and any(
        marker in text for marker in (
            "not valid", "exceed", "maximum", "limit", "greater than", "less than",
        )
    )


def build_copypaste_prompt(custom_prompt: str = "") -> str:
    """Prompt final = hard prompt + demande custom optionnelle."""
    custom = (custom_prompt or "").strip()
    if not custom:
        return HARD_PROMPT
    sep = "" if custom.endswith((".", "!", "?")) else "."
    return f"{HARD_PROMPT} {custom}{sep}"


def pick_bank_videos(
    bank: list[Option], count: int, rng: random.Random | None = None
) -> list[str]:
    """Pioche `count` vidéos AU HASARD dans la banque (tirage pondéré, avec
    remise : la banque peut être plus petite que le batch demandé)."""
    if not bank:
        return []
    rng = rng or random.Random()
    return [weighted_draw(bank, rng).text for _ in range(count)]
