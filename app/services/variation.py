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
from app.services.dialogue import render_for_prompt, split_transcript_halves
from app.services.scene_style import apply_scene_style

SLOT_PATTERN = re.compile(r"\{(\w+)\}")
CAPTION_SLOT = "caption"
DIALOGUE_SLOT = "dialogue"

# Format long 30 s : catégorie dédiée. La scène ET les paroles viennent d'un
# template reverse-engineeré (transcript apparié), jamais des banques
# background/dialogue. Seuls la model et l'outfit varient d'une vidéo à l'autre.
LONG_FORM_CATEGORY = "storytelling_long"
# 2 clips de 15 s enchaînés = 30 s (Seedance 2.0 Fast plafonne à 15 s/génération).
LONG_FORM_CLIP_S = 15
LONG_FORM_TOTAL_S = LONG_FORM_CLIP_S * 2

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
    # Paroles transcrites de la vidéo de référence (format long 30 s). None pour
    # un template classique dont les dialogues viennent de la banque.
    transcript: str | None = None


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
    # Format long 30 s : prompt + paroles du 2e clip de 15 s (None pour 1 clip).
    filled_prompt_2: str | None = None
    dialogue_script_2: str | None = None


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


_DOUBLE_WEARING_RE = re.compile(r"\bwearing\s+wearing\b", re.IGNORECASE)


def fill_template(template_text: str, values: dict[str, str]) -> str:
    """Remplit les slots connus, laisse les inconnus intacts (le designer de
    template voit immédiatement ce qui n'est pas résolu).

    Corrige le doublon « wearing wearing » : un template reverse-engineeré peut
    écrire « wearing {outfit} » alors que le texte d'outfit commence DÉJÀ par
    « wearing » → on réduit à un seul « wearing »."""

    def replace(match: re.Match) -> str:
        return values.get(match.group(1), match.group(0))

    filled = SLOT_PATTERN.sub(replace, template_text)
    return _DOUBLE_WEARING_RE.sub("wearing", filled)


def _compose_one(
    category: str,
    pools: CategoryPools,
    characteristics: list[composer.CharacteristicInput],
    face_reference_url: str,
    max_refs: int,
    rng: random.Random,
    custom_prompt: str = "",
    omit_background: bool = False,
) -> ComposedItem:
    if category == LONG_FORM_CATEGORY:
        return _compose_long_form(
            category, pools, characteristics, face_reference_url, max_refs, rng,
            custom_prompt=custom_prompt,
        )
    template = weighted_draw(pools.templates, rng)
    outfit = weighted_draw(pools.outfits, rng)
    # On ne tire un décor QUE si le template a le slot {background} (un template
    # dont la scène est déjà écrite — ex. « file d'attente au McDo » — ne doit pas
    # pioché un décor aléatoire qui parasiterait la scène). Idem si case « pas de background ».
    wants_background = "{background}" in template.template_text
    background = (
        weighted_draw(pools.backgrounds, rng)
        if wants_background and not omit_background
        else None
    )

    # Le caption sert au BANDEAU snapchat incrusté par FFmpeg (texte EXACT), pas au
    # prompt Seedance. On tire un caption pour la catégorie snapchat (ou si un
    # template le demande). Optionnel : banque vide → pas de bandeau, sans échec.
    wants_caption = category == "snapchat" or f"{{{CAPTION_SLOT}}}" in template.template_text
    caption = weighted_draw(pools.captions, rng) if wants_caption else None

    # Dialogue OPTIONNEL : un template « speaking » sans banque de dialogues ne
    # bloque plus la génération (le slot {dialogue} reste vide — utile si tu écris
    # la réplique directement dans le texte du template). Ajoute des dialogues
    # dans la banque pour varier automatiquement.
    dialogue = weighted_draw(pools.dialogues, rng) if template.speaking else None

    values = {
        "outfit": outfit.text if outfit else "",
        "background": background.text if background else "",
        # {characteristics} est laissé intact ici : inject_characteristics
        # remplit ce slot lui-même (ou ajoute en fin de prompt s'il est absent)
        DIALOGUE_SLOT: render_for_prompt(dialogue.text) if dialogue else "",
        # Le texte du caption n'est JAMAIS injecté dans le prompt : Seedance le
        # rendrait en dur et mal (texte IA illisible / mauvais contenu). Il est
        # incrusté proprement par FFmpeg via caption_text. Slot toujours vidé.
        CAPTION_SLOT: "",
    }
    # 1 caractéristique aléatoire par item + les récurrentes (ex. tatouage)
    active = composer.select_active_characteristics(characteristics, rng)
    active_ids = sorted(c.id for c in active)

    prompt = fill_template(template.template_text, values)
    prompt = composer.inject_characteristics(prompt, active)
    # Demande custom (one-shot) + directive caméra (fixe hors snapchat) / voix / pas de musique.
    # Directive voix liée à l'INTENTION (case speaking) : marche même si tu écris la
    # réplique directement dans le texte du template, sans passer par la banque.
    prompt = apply_scene_style(prompt, category, template.speaking, custom_prompt)

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


def _compose_long_form(
    category: str,
    pools: CategoryPools,
    characteristics: list[composer.CharacteristicInput],
    face_reference_url: str,
    max_refs: int,
    rng: random.Random,
    custom_prompt: str = "",
) -> ComposedItem:
    """Compose UN item long 30 s (2 clips de 15 s).

    Scène + paroles proviennent d'un template reverse-engineeré (avec transcript) :
    même décor, même tenue sur les 2 clips ; seuls model + outfit varient. Le
    transcript est coupé en 2 → moitié 1 sur le clip 1, moitié 2 sur le clip 2.
    Aucune banque background/dialogue/caption n'est piochée.
    """
    # Seuls les templates PORTEURS de transcript sont éligibles (les paroles
    # viennent d'eux, jamais des dialogues manuels).
    usable = [t for t in pools.templates if (t.transcript or "").strip()]
    template = weighted_draw(usable, rng)
    if template is None:
        raise ComposeError(
            "storytelling_long : aucun template avec transcript (reverse-engineer "
            "d'abord des vidéos de référence dans cette catégorie)."
        )
    outfit = weighted_draw(pools.outfits, rng)
    active = composer.select_active_characteristics(characteristics, rng)
    active_ids = sorted(c.id for c in active)

    first_half, second_half = split_transcript_halves(template.transcript)

    def build_prompt(dialogue_raw: str) -> str:
        values = {
            "outfit": outfit.text if outfit else "",
            # Décor déjà écrit dans la scène reverse-engineerée → slot vidé.
            "background": "",
            DIALOGUE_SLOT: render_for_prompt(dialogue_raw) if dialogue_raw else "",
            CAPTION_SLOT: "",
        }
        p = fill_template(template.template_text, values)
        p = composer.inject_characteristics(p, active)
        # speaking=True : c'est un format parlé (paroles issues du transcript).
        return apply_scene_style(p, category, True, custom_prompt)

    extra_refs = [outfit.image_url] if outfit and outfit.image_url else []
    refs = composer.select_reference_images(
        face_reference_url, active, extra_refs=extra_refs, max_refs=max_refs
    )

    return ComposedItem(
        category=category,
        template_id=template.id,
        outfit_id=outfit.id if outfit else None,
        background_id=None,
        dialogue_id=None,
        caption_id=None,
        caption_text=None,
        filled_prompt=build_prompt(first_half),
        dialogue_script=first_half or None,
        filled_prompt_2=build_prompt(second_half),
        dialogue_script_2=second_half or None,
        reference_image_urls=refs,
        combo_hash=composer.combo_hash(
            {
                "category": category,
                "template": template.id,
                "outfit": outfit.id if outfit else None,
                "characteristics": active_ids,
            }
        ),
        speaking=True,
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
    custom_prompt: str = "",
    omit_background: bool = False,
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
                    category, pools, characteristics, face_reference_url, max_refs, rng,
                    custom_prompt=custom_prompt, omit_background=omit_background,
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
