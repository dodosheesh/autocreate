"""Moteur de composition + variation (brief §5) — logique PURE.

Aucun accès DB : les banques sont passées en listes d'options pondérées,
le wrapper Celery (workers/tasks.compose_job) fait le pont avec Postgres.

Par item : tirage pondéré template + outfit + background (+ dialogue si
speaking, + caption si le template a le slot), injection des caractéristiques,
combo_hash pour la dédup intra-job (re-tirage si déjà vu), remplissage des
slots, résolution des images de référence (cap Seedance).
"""

import random
import re
from dataclasses import dataclass, field

from app.services import composer
from app.services.dialogue import render_for_prompt

SLOT_PATTERN = re.compile(r"\{(\w+)\}")
CAPTION_SLOT = "caption"
DIALOGUE_SLOT = "dialogue"

MAX_DRAW_ATTEMPTS = 25


class ComposeError(ValueError):
    """Banque manquante pour composer la catégorie demandée."""


@dataclass(frozen=True)
class Option:
    """Une entrée de banque, pondérée pour le tirage."""

    id: str
    weight: float = 1.0
    text: str = ""  # contribution au prompt ({outfit}, {background}, {caption}…)
    image_url: str | None = None  # ref envoyée à Seedance si présente
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TemplateOption:
    id: str
    template_text: str
    speaking: bool
    weight: float = 1.0


@dataclass(frozen=True)
class CategoryPools:
    templates: list[TemplateOption]
    outfits: list[Option]
    backgrounds: list[Option]
    dialogues: list[Option]  # Option.text = raw_text taggé [H]/[F]
    captions: list[Option]


@dataclass(frozen=True)
class ComposedItem:
    category: str
    template_id: str
    outfit_id: str | None
    background_id: str | None
    dialogue_id: str | None
    caption_id: str | None
    caption_text: str | None
    filled_prompt: str
    dialogue_script: str | None
    reference_image_urls: list[str]
    combo_hash: str
    speaking: bool
    characteristic_ids: list[str]  # caractéristiques réellement appliquées à CET item


@dataclass(frozen=True)
class ComposeResult:
    items: list[ComposedItem]
    # combos uniques épuisés avant d'atteindre le count demandé (jamais silencieux)
    shortfall_per_category: dict[str, int]


def weighted_draw(options: list, rng: random.Random):
    if not options:
        return None
    weights = [max(getattr(o, "weight", 1.0), 0.0) for o in options]
    if sum(weights) <= 0:
        weights = [1.0] * len(options)
    return rng.choices(options, weights=weights, k=1)[0]


def fill_template(template_text: str, values: dict[str, str]) -> str:
    """Remplit les slots connus, laisse les inconnus intacts (le designer de
    template voit immédiatement ce qui n'est pas résolu)."""

    def replace(match: re.Match) -> str:
        return values.get(match.group(1), match.group(0))

    return SLOT_PATTERN.sub(replace, template_text)


def _compose_one(
    category: str,
    pools: CategoryPools,
    characteristics: list[composer.CharacteristicInput],
    face_reference_url: str,
    max_refs: int,
    rng: random.Random,
) -> ComposedItem:
    template = weighted_draw(pools.templates, rng)
    outfit = weighted_draw(pools.outfits, rng)
    background = weighted_draw(pools.backgrounds, rng)

    wants_caption = f"{{{CAPTION_SLOT}}}" in template.template_text
    caption = weighted_draw(pools.captions, rng) if wants_caption else None
    if wants_caption and caption is None:
        raise ComposeError(f"Catégorie {category} : slot {{caption}} mais banque captions vide")

    dialogue = None
    if template.speaking:
        dialogue = weighted_draw(pools.dialogues, rng)
        if dialogue is None:
            raise ComposeError(
                f"Catégorie {category} : template speaking mais banque dialogues vide"
            )

    values = {
        "outfit": outfit.text if outfit else "",
        "background": background.text if background else "",
        # {characteristics} est laissé intact ici : inject_characteristics
        # remplit ce slot lui-même (ou ajoute en fin de prompt s'il est absent)
        DIALOGUE_SLOT: render_for_prompt(dialogue.text) if dialogue else "",
        CAPTION_SLOT: caption.text if caption else "",
    }
    # 1 caractéristique aléatoire par item + les récurrentes (ex. tatouage)
    active = composer.select_active_characteristics(characteristics, rng)
    active_ids = sorted(c.id for c in active)

    prompt = fill_template(template.template_text, values)
    prompt = composer.inject_characteristics(prompt, active)

    extra_refs = [o.image_url for o in (outfit, background) if o and o.image_url]
    refs = composer.select_reference_images(
        face_reference_url, active, extra_refs=extra_refs, max_refs=max_refs
    )

    return ComposedItem(
        category=category,
        template_id=template.id,
        outfit_id=outfit.id if outfit else None,
        background_id=background.id if background else None,
        dialogue_id=dialogue.id if dialogue else None,
        caption_id=caption.id if caption else None,
        caption_text=caption.text if caption else None,
        filled_prompt=prompt,
        dialogue_script=dialogue.text if dialogue else None,
        reference_image_urls=refs,
        combo_hash=composer.combo_hash(
            {
                "category": category,
                "template": template.id,
                "outfit": outfit.id if outfit else None,
                "background": background.id if background else None,
                "dialogue": dialogue.id if dialogue else None,
                "caption": caption.id if caption else None,
                # la caractéristique aléatoire fait partie de la variante (dédup + variété)
                "characteristics": active_ids,
            }
        ),
        speaking=template.speaking,
        characteristic_ids=active_ids,
    )


def compose_batch(
    counts_per_category: dict[str, int],
    pools_by_category: dict[str, CategoryPools],
    characteristics: list[composer.CharacteristicInput],
    face_reference_url: str,
    max_refs: int = 12,
    rng: random.Random | None = None,
    seen_hashes: set[str] | None = None,
) -> ComposeResult:
    """Compose tous les items d'un job, dédupliqués par combo_hash.

    Si l'espace de combos uniques s'épuise (MAX_DRAW_ATTEMPTS tirages
    consécutifs déjà vus), la catégorie s'arrête et le manque est reporté
    dans shortfall_per_category — jamais de doublon, jamais de cap silencieux.
    """
    rng = rng or random.Random()
    seen = seen_hashes if seen_hashes is not None else set()
    items: list[ComposedItem] = []
    shortfall: dict[str, int] = {}

    for category, count in counts_per_category.items():
        if count <= 0:
            continue
        pools = pools_by_category.get(category)
        # Catégorie sans template prêt : on la REPORTE en shortfall et on continue.
        # (Ne JAMAIS faire échouer tout le batch parce qu'un seul style est vide —
        # sinon « une vidéo de chaque style » plante dès qu'un style n'a pas de template.)
        if pools is None or not pools.templates:
            shortfall[category] = count
            continue
        produced = 0
        attempts_since_new = 0
        while produced < count:
            try:
                item = _compose_one(
                    category, pools, characteristics, face_reference_url, max_refs, rng
                )
            except ComposeError:
                # Template inutilisable (parlant sans dialogues, slot caption sans
                # captions…) : tirage gâché. On retente d'autres templates ; si
                # l'espace s'épuise, on reporte le reste en shortfall sans planter.
                attempts_since_new += 1
                if attempts_since_new >= MAX_DRAW_ATTEMPTS:
                    shortfall[category] = count - produced
                    break
                continue
            if item.combo_hash in seen:
                attempts_since_new += 1
                if attempts_since_new >= MAX_DRAW_ATTEMPTS:
                    shortfall[category] = count - produced
                    break
                continue
            seen.add(item.combo_hash)
            items.append(item)
            produced += 1
            attempts_since_new = 0

    return ComposeResult(items=items, shortfall_per_category=shortfall)


def outfit_option(id: str, tags: list[str], image_url: str, weight: float) -> Option:
    text = f"wearing {', '.join(tags)}" if tags else "wearing the referenced outfit"
    return Option(id=id, weight=weight, text=text, image_url=image_url)


def background_option(id: str, tags: list[str], image_url: str, weight: float) -> Option:
    text = ", ".join(tags) if tags else "the referenced background"
    return Option(id=id, weight=weight, text=text, image_url=image_url)
