"""Composition d'items image (feature Pictures) — logique PURE.

Même philosophie que le moteur de variation vidéo :
- tirage pondéré d'un prompt (banque persistante reverse-engineerée) + d'un
  outfit optionnel qui se mélange au prompt de base ;
- injection des caractéristiques de la model dans le prompt ;
- refs images = visage + caractéristiques + outfit (cap nano banana 10) ;
- combo_hash pour la dédup intra-job.
"""

import random
from dataclasses import dataclass

from app.services import composer
from app.services.variation import Option, weighted_draw


@dataclass(frozen=True)
class ComposedPicture:
    prompt_id: str
    outfit_id: str | None
    filled_prompt: str
    reference_image_urls: list[str]
    combo_hash: str
    characteristic_ids: list[str]  # caractéristiques réellement appliquées à CETTE image


@dataclass(frozen=True)
class PictureComposeResult:
    items: list[ComposedPicture]
    shortfall: int  # combos uniques épuisés avant d'atteindre le count


MAX_DRAW_ATTEMPTS = 25


class PictureComposeError(ValueError):
    pass


def _merge_outfit(prompt_text: str, outfit: Option | None) -> str:
    if outfit is None or not outfit.text:
        return prompt_text
    sep = "" if prompt_text.rstrip().endswith((".", "!", "?")) else "."
    return f"{prompt_text.rstrip()}{sep} She is {outfit.text}."


def compose_pictures(
    count: int,
    prompts: list[Option],  # Option.text = prompt_text de la banque
    outfits: list[Option],
    characteristics: list[composer.CharacteristicInput],
    face_reference_url: str,
    max_refs: int = 10,
    rng: random.Random | None = None,
) -> PictureComposeResult:
    if not prompts:
        raise PictureComposeError("Aucun prompt en banque (reverse-engineerer une image d'abord)")
    rng = rng or random.Random()
    seen: set[str] = set()
    items: list[ComposedPicture] = []
    attempts_since_new = 0

    while len(items) < count:
        prompt = weighted_draw(prompts, rng)
        outfit = weighted_draw(outfits, rng) if outfits else None
        # PHOTO : uniquement les caractéristiques cochées « seedream » (+ visage),
        # aucune du pool aléatoire.
        active = composer.select_photo_characteristics(characteristics)
        active_ids = sorted(c.id for c in active)
        combo = composer.combo_hash(
            {
                "prompt": prompt.id,
                "outfit": outfit.id if outfit else None,
                "characteristics": active_ids,
            }
        )
        if combo in seen:
            attempts_since_new += 1
            if attempts_since_new >= MAX_DRAW_ATTEMPTS:
                break
            continue
        seen.add(combo)
        attempts_since_new = 0

        text = _merge_outfit(prompt.text, outfit)
        text = composer.inject_characteristics(text, active)
        extra_refs = [outfit.image_url] if outfit and outfit.image_url else []
        refs = composer.select_reference_images(
            face_reference_url, active, extra_refs=extra_refs, max_refs=max_refs
        )
        items.append(
            ComposedPicture(
                prompt_id=prompt.id,
                outfit_id=outfit.id if outfit else None,
                filled_prompt=text,
                reference_image_urls=refs,
                combo_hash=combo,
                characteristic_ids=active_ids,
            )
        )
    return PictureComposeResult(items=items, shortfall=max(0, count - len(items)))
