"""Feature copypaste (vidéo → vidéo) — logique PURE.

Seedance 2 accepte des vidéos de référence : on lui envoie la vidéo source +
la photo visage de la model avec un prompt fixe de remplacement (+ demande
custom optionnelle). Pas de banques templates/outfits/backgrounds : la scène,
l'action et le décor sont ceux de la vidéo de référence.
"""

import random

from app.services.variation import Option, weighted_draw

# Prompt FIXE de la feature : la fille de la vidéo est remplacée par la model
# (la photo visage part en référence image à côté de la vidéo).
HARD_PROMPT = "Replace the girl in the video with the girl in the picture."


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
