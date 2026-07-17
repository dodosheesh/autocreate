"""Composition d'items image (feature Pictures) — logique PURE.

Même philosophie que le moteur de variation vidéo :
- tirage pondéré d'un prompt (banque persistante reverse-engineerée) + d'un
  outfit optionnel qui se mélange au prompt de base ;
- injection des caractéristiques de la model dans le prompt ;
- refs images = visage + caractéristiques + outfit (cap nano banana 10) ;
- anti-statique : une variante de pose/action tirée au sort par photo (sucette,
  bulle de chewing-gum, duck face…) pour casser les poses figées des banques ;
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

# ANTI-STATIQUE PHOTO (pendant de _LIVELINESS côté vidéo) : les prompts de banque
# donnent souvent des poses figées → on tire une variante de pose/action ludique
# par photo pour rendre le feed vivant et varié.
PHOTO_POSES: list[Option] = [
    Option(id="pose_lollipop_heart", text="playfully licking a red heart-shaped lollipop while looking at the camera"),
    Option(id="pose_lollipop_pink", text="licking a big round pink lollipop with a cheeky look"),
    Option(id="pose_bubble_gum", text="blowing a big pink bubble with her chewing gum"),
    Option(id="pose_duck_face", text="making an exaggerated playful duck face at the camera"),
    Option(id="pose_kiss_face", text="making a kiss face and blowing a kiss toward the camera"),
    Option(id="pose_wink_tongue", text="winking at the camera and playfully sticking her tongue out"),
    Option(id="pose_peace_sign", text="flashing a peace sign next to her eye with her head slightly tilted"),
    Option(id="pose_hair_play", text="playing with a strand of her hair with a cute smile"),
    Option(id="pose_pouty_cheeks", text="puffing her cheeks in a cute pouty expression"),
    Option(id="pose_mid_laugh", text="caught mid-laugh, head slightly tilted back, natural and candid"),
    Option(id="pose_hip_pop", text="one hand on her hip, hip popped to the side, with a confident smirk"),
]


class PictureComposeError(ValueError):
    pass


def _merge_outfit(prompt_text: str, outfit: Option | None) -> str:
    if outfit is None or not outfit.text:
        return prompt_text
    sep = "" if prompt_text.rstrip().endswith((".", "!", "?")) else "."
    return f"{prompt_text.rstrip()}{sep} She is {outfit.text}."


def _apply_pose(text: str, pose: Option | None) -> str:
    """Injecte la variante de pose (remplace la pose statique du prompt de banque)."""
    if pose is None or not pose.text:
        return text
    sep = "" if text.rstrip().endswith((".", "!", "?")) else "."
    return f"{text.rstrip()}{sep} Instead of a stiff static pose, she is {pose.text}."


def _apply_style(text: str, style_suffix: str) -> str:
    """Ajoute le modificateur de style (ex. rendu amateur) à la fin du prompt."""
    style_suffix = (style_suffix or "").strip()
    if not style_suffix:
        return text
    sep = " " if text.rstrip().endswith((".", "!", "?")) else ". "
    return f"{text.rstrip()}{sep}{style_suffix}"


def compose_pictures(
    count: int,
    prompts: list[Option],  # Option.text = prompt_text de la banque
    outfits: list[Option],
    characteristics: list[composer.CharacteristicInput],
    face_reference_url: str,
    max_refs: int = 10,
    rng: random.Random | None = None,
    style_suffix: str = "",
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
        # Pose tirée hors combo_hash : pure variété de rendu, la dédup reste
        # prompt × outfit × caractéristiques.
        text = _apply_pose(text, weighted_draw(PHOTO_POSES, rng))
        text = _apply_style(text, style_suffix)
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
