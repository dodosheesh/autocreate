"""Bibliothèque de templates de prompts prête à l'emploi.

Ce que je fournis (mise en scène) : décors, cadrages, angles caméra, actions,
ambiances, lumière, style — par catégorie. Ce que l'utilisateur dépose lui-même :
le texte parlé (slot {dialogue}, taggé [H]/[F]) et les captions (slot {caption}).

Les templates utilisent les slots résolus par le moteur :
  {outfit} {background} {characteristics} {dialogue} {caption}
- {outfit} / {background} : tirés des banques (variété automatique)
- {characteristics} : injecté (traits de la model)
- {dialogue} : rendu naturel du script taggé (catégories `speaking`)
- {caption} : texte de la banque captions (catégorie snapchat surtout)

`load_default_templates(db, tenant_id)` insère ceux qui manquent (dédup par
texte) et renvoie le nombre ajouté.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import PromptTemplate

# (category, template_text, speaking)
# Règle de mise en scène : contenu réseaux sociaux réaliste, PAS de rendu « IA ».
# Elle se filme elle-même au téléphone (posé/à bout de bras). La contrainte caméra
# (fixe, aucun zoom, aucun mouvement — sauf snapchat filmé par un tiers), la
# « vraie voix » et « pas de musique » sont ajoutées AUTOMATIQUEMENT à la
# composition (app/services/scene_style.py) — inutile de les répéter ici.
DEFAULT_TEMPLATES: list[tuple[str, str, bool]] = [
    # ---------------- skit (elle parle face caméra, comme à un pote) ----------------
    (
        "skit",
        "Vertical 9:16 phone video. A young woman {outfit} stands in {background}, filming "
        "herself on her propped-up phone. She talks straight to the camera like she is "
        "talking to a friend, relaxed and playful, natural face and hand gestures. "
        "{characteristics}. {dialogue}",
        True,
    ),
    (
        "skit",
        "9:16 selfie video. She {outfit} in {background} sets her phone down and steps back "
        "into frame, then talks to the camera with casual, expressive everyday energy. "
        "{characteristics}. {dialogue}",
        True,
    ),
    # Reveal tenu par un outfit LONG (hoodie oversize / haut qui descend aux cuisses).
    # L'action de lever brièvement le bas du haut est décrite ; CE qui est montré et
    # la réplique restent portés par {outfit} et {dialogue} (tes propres textes).
    (
        "skit",
        "Vertical 9:16 phone video. A woman {outfit} — a long oversized hoodie that falls to "
        "her thighs — stands in {background} filming herself on her propped-up phone. She "
        "talks to the camera, and on the beat she briefly lifts the hem of the oversized "
        "hoodie for a split second and lets it drop right back down, playful and quick. "
        "{characteristics}. {dialogue}",
        True,
    ),
    # ---------------- storytelling (storytime face caméra) ----------------
    (
        "storytelling",
        "Vertical 9:16 phone video, storytime to camera. A young woman {outfit} sits in "
        "{background}, close intimate framing, talking sincerely to her phone about something "
        "personal, small natural micro-expressions. {characteristics}. {dialogue}",
        True,
    ),
    (
        "storytelling",
        "9:16 selfie video. She {outfit} in {background} tells a personal story straight to "
        "the camera, calm and genuine, soft natural indoor light. {characteristics}. {dialogue}",
        True,
    ),
    # ---------------- showing_body (outfit check, peu ou pas parlé) ----------------
    (
        "showing_body",
        "Vertical 9:16 phone video, outfit check. A woman {outfit} stands in {background} and "
        "poses for her propped-up phone, turning and shifting her weight to show the fit, "
        "confident and casual. {characteristics}.",
        False,
    ),
    (
        "showing_body",
        "9:16 mirror selfie video, phone in hand. A woman {outfit} in {background} films "
        "herself in the mirror doing a few relaxed poses to show the outfit. {characteristics}.",
        False,
    ),
    # ---------------- micro_trottoir (micro-trottoir / interview de rue) ----------------
    (
        "micro_trottoir",
        "Vertical 9:16 phone video, street-interview style. A woman {outfit} in {background} "
        "answers an off-screen interviewer, candid and natural, reacting and gesturing as she "
        "replies. {characteristics}. {dialogue}",
        True,
    ),
    (
        "micro_trottoir",
        "9:16 vox-pop clip. She {outfit} stopped in {background}, a mic just out of frame, "
        "spontaneous and real as she answers. {characteristics}. {dialogue}",
        True,
    ),
    # ---------------- podcast (au micro, face caméra) ----------------
    (
        "podcast",
        "Vertical 9:16 phone video, podcast clip. A woman {outfit} sits at a microphone in "
        "{background}, relaxed conversational body language, talking to the camera and "
        "gesturing naturally. {characteristics}. {dialogue}",
        True,
    ),
    (
        "podcast",
        "9:16 podcast moment. She {outfit} at the mic in {background} delivers a take with "
        "natural hand gestures and eye contact with the camera. {characteristics}. {dialogue}",
        True,
    ),
    # ---------------- snapchat (candide, filmé par un tiers, barre de légende) ----------------
    # SEULE catégorie où la caméra peut bouger/zoomer (vidéo filmée par quelqu'un d'autre).
    (
        "snapchat",
        "Vertical 9:16 clip filmed candidly by someone else on a phone. A woman {outfit} in "
        "{background} in an everyday social moment, realistic phone-camera look, a Snapchat-style "
        "caption bar across the top. Caption: {caption}. {characteristics}.",
        False,
    ),
    (
        "snapchat",
        "9:16 candid clip filmed from across the room. She {outfit} in {background} in a mundane "
        "moment, natural available light, Snapchat caption bar up top. Caption: {caption}. "
        "{characteristics}.",
        False,
    ),
]


def ensure_slots(template_text: str, speaking: bool) -> str:
    """Garantit que le template issu du reverse-engineering vidéo porte bien les
    slots nécessaires pour que les assets/caractéristiques/dialogue se mélangent,
    même si le LLM les a oubliés."""
    text = template_text.strip()
    if "{outfit}" not in text:
        sep = "" if text.endswith((".", "!", "?")) else "."
        text = f"{text}{sep} She is {{outfit}}."
    if "{background}" not in text:
        text = f"{text} Background: {{background}}."
    if "{characteristics}" not in text:
        text = f"{text} {{characteristics}}."
    if speaking and "{dialogue}" not in text:
        text = f"{text} {{dialogue}}"
    return text


def load_default_templates(db: Session, tenant_id: str) -> int:
    """Insère les templates par défaut manquants pour ce tenant (dédup par
    (catégorie, texte)). Renvoie le nombre réellement ajouté."""
    existing = {
        (t.category, t.template_text)
        for t in db.scalars(
            select(PromptTemplate).where(PromptTemplate.tenant_id == tenant_id)
        ).all()
    }
    added = 0
    for category, text, speaking in DEFAULT_TEMPLATES:
        if (category, text) in existing:
            continue
        db.add(
            PromptTemplate(
                tenant_id=tenant_id,
                category=category,
                template_text=text,
                speaking=speaking,
                weight=1.0,
            )
        )
        added += 1
    db.commit()
    return added
