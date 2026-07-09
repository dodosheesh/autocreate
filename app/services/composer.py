"""Composition d'un item (Phase 1, mono-catégorie, prompt fourni à la main).

Deux responsabilités déjà définitives (elles serviront telles quelles au
moteur de variation de la Phase 2) :

1. Injection des caractéristiques spéciales de la model dans le prompt
   (slot {characteristics}, sinon insertion en fin de description de scène).
2. Sélection des images de référence : visage + photos des caractéristiques
   par priorité, dans la limite des 12 refs Seedance 2.0.

Plus le combo_hash servant à la dédup des variantes.
"""

import hashlib
import json
from dataclasses import dataclass

CHARACTERISTICS_SLOT = "{characteristics}"


@dataclass(frozen=True)
class CharacteristicInput:
    label: str
    reference_image_url: str
    injection_hint: str
    priority: int = 0
    always_include: bool = True


def inject_characteristics(prompt: str, characteristics: list[CharacteristicInput]) -> str:
    """Insère les injection_hints dans le prompt.

    Si le template contient le slot {characteristics}, les hints y sont placés
    (c'est là que le template designer a jugé le placement naturel). Sinon les
    hints sont ajoutés en phrase après la description, jamais au milieu d'une
    instruction de dialogue.
    """
    included = sorted(
        (c for c in characteristics if c.always_include),
        key=lambda c: c.priority,
    )
    hints = ", ".join(c.injection_hint.strip().rstrip(".") for c in included)
    if CHARACTERISTICS_SLOT in prompt:
        return prompt.replace(CHARACTERISTICS_SLOT, hints)
    if not hints:
        return prompt
    separator = "" if prompt.rstrip().endswith((".", "!", "?")) else "."
    return f"{prompt.rstrip()}{separator} {hints[0].upper()}{hints[1:]}."


def select_reference_images(
    face_reference_url: str,
    characteristics: list[CharacteristicInput],
    extra_refs: list[str] | None = None,
    max_refs: int = 12,
) -> list[str]:
    """Face d'abord (jamais évincée), puis caractéristiques par priorité,
    puis refs additionnelles (outfit, background) — coupé à max_refs.
    Dédoublonne en préservant l'ordre."""
    ordered = [face_reference_url]
    ordered += [
        c.reference_image_url
        for c in sorted(
            (c for c in characteristics if c.always_include), key=lambda c: c.priority
        )
    ]
    ordered += extra_refs or []
    seen: set[str] = set()
    result: list[str] = []
    for url in ordered:
        if url and url not in seen:
            seen.add(url)
            result.append(url)
        if len(result) == max_refs:
            break
    return result


def combo_hash(payload: dict) -> str:
    """Hash stable d'un combo (template/outfit/background/dialogue/caption)
    pour la dédup intra-job."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
