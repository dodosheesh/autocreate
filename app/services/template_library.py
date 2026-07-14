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
        "Vertical 9:16 phone video. A young woman {outfit} in {background} talks straight to "
        "the camera like she is talking to a friend, lively and playful, with expressive face "
        "and animated hand gestures, shifting her pose and moving naturally the whole time. "
        "{characteristics}. {dialogue}",
        True,
    ),
    (
        "skit",
        "9:16 selfie video. A woman {outfit} in {background} talks to the camera with casual, "
        "expressive everyday energy — playful gestures, a little hip sway, cute expressions, "
        "always moving. {characteristics}. {dialogue}",
        True,
    ),
    # Reveal tenu par un outfit LONG (hoodie oversize / haut qui descend aux cuisses).
    (
        "skit",
        "Vertical 9:16 phone video. A woman {outfit} — a long oversized hoodie that falls to "
        "her thighs — in {background} talks to the camera, playful and animated, and on the "
        "beat she briefly lifts the hem of the oversized hoodie for a split second and lets it "
        "drop right back down, quick and cheeky. {characteristics}. {dialogue}",
        True,
    ),
    # ---------------- storytelling (storytime face caméra) ----------------
    (
        "storytelling",
        "Vertical 9:16 phone video, storytime to camera. A young woman {outfit} in {background} "
        "talks sincerely to her phone about something personal, expressive and animated with "
        "lively hand gestures and warm micro-expressions, never staying still. "
        "{characteristics}. {dialogue}",
        True,
    ),
    (
        "storytelling",
        "9:16 selfie video. A woman {outfit} in {background} tells a personal story to the "
        "camera, genuine and animated, gesturing and shifting her pose as she talks. "
        "{characteristics}. {dialogue}",
        True,
    ),
    # ---------------- showing_body (outfit check — DOIT bouger, pas figée) ----------------
    (
        "showing_body",
        "Vertical 9:16 phone video, playful outfit check. A woman {outfit} in {background} "
        "shows off her outfit with a lively little routine — turning, a hip sway, a hand on "
        "her hip, a slow spin, tilting her head, cute confident poses that flow one into the "
        "next, always moving. {characteristics}.",
        False,
    ),
    (
        "showing_body",
        "9:16 full-body outfit check. A woman {outfit} in {background} does a playful sequence "
        "of poses to show the fit — striking a pose, shifting her weight, lifting her back leg "
        "slightly, blowing a bubble with gum, running a hand through her hair, staying lively "
        "and never frozen. {characteristics}.",
        False,
    ),
    # ---------------- micro_trottoir (micro-trottoir / interview de rue) ----------------
    (
        "micro_trottoir",
        "Vertical 9:16 phone video, street-interview style. A woman {outfit} in {background} "
        "answers an off-screen interviewer, candid and animated, reacting, laughing and "
        "gesturing lively as she replies. {characteristics}. {dialogue}",
        True,
    ),
    (
        "micro_trottoir",
        "9:16 vox-pop clip. A woman {outfit} in {background}, a mic just out of frame, answers "
        "spontaneously with expressive reactions and hand gestures, moving naturally. "
        "{characteristics}. {dialogue}",
        True,
    ),
    # ---------------- podcast (VRAI plateau podcast — pas de {background} : le décor est écrit) ----------------
    (
        "podcast",
        "Vertical 9:16 real podcast clip. A woman {outfit} sits at a podcast microphone in a "
        "real podcast studio (soft warm studio lighting, acoustic foam panels and a cozy set "
        "behind her), talking like a guest on a podcast — animated, leaning in, expressive "
        "hand gestures and eye contact, lively the whole time. {characteristics}. {dialogue}",
        True,
    ),
    (
        "podcast",
        "Vertical 9:16 podcast moment. A woman {outfit} at a big studio podcast mic in a warm "
        "podcast set, delivers a take with animated hands, head movement and expressive "
        "reactions, never sitting still. {characteristics}. {dialogue}",
        True,
    ),
    # ---------------- snapchat (candide, filmé par un tiers) ----------------
    # SEULE catégorie où la caméra peut bouger/zoomer (vidéo filmée par quelqu'un d'autre).
    # PAS de texte demandé à Seedance : le bandeau Snapchat (ton texte exact) est
    # incrusté par-dessus par l'appli (FFmpeg) — on laisse juste l'espace en haut.
    (
        "snapchat",
        "Vertical 9:16 clip filmed candidly by someone else on a phone. A woman {outfit} in "
        "{background} in an everyday social moment, realistic phone-camera look. Leave the top "
        "area clean with empty space and render NO text or caption in the video. {characteristics}.",
        False,
    ),
    (
        "snapchat",
        "9:16 candid clip filmed from across the room. She {outfit} in {background} in a mundane "
        "moment, natural available light. Keep the top area empty and render NO on-screen text "
        "or caption. {characteristics}.",
        False,
    ),
    # Scénarios candides « POV filmé de loin » (~20 m, léger zoom lent) : la scène est
    # déjà écrite → PAS de slot {background} (aucun décor aléatoire n'est tiré).
    (
        "snapchat",
        "Vertical 9:16 candid clip filmed discreetly from about 20 meters away on a phone, "
        "with a slight slow zoom in on her. A woman {outfit} waits in line at a fast-food "
        "counter (McDonald's / Wendy's style), glancing at the menu and her phone, unaware she "
        "is being filmed. Realistic phone-camera look, render NO on-screen text. {characteristics}.",
        False,
    ),
    (
        "snapchat",
        "Vertical 9:16 candid clip filmed from a distance (~20 meters) with a slight zoom, as if "
        "recorded discreetly from the crowd. A woman {outfit} sits in stadium bleachers, casually "
        "eating a hotdog and watching the game, unaware of the camera. Realistic phone-camera "
        "feel, render NO on-screen text. {characteristics}.",
        False,
    ),
    (
        "snapchat",
        "Vertical 9:16 POV clip filmed from inside a swimming pool by a guy holding a phone, from "
        "a distance with a slight zoom. A woman {outfit} lies on a poolside lounger sunbathing, "
        "relaxed, unaware she is being filmed from the water. Bright sunny day, realistic "
        "phone-camera look, render NO on-screen text. {characteristics}.",
        False,
    ),
    (
        "snapchat",
        "Vertical 9:16 candid street clip filmed from across the street (~20 meters) with a slight "
        "zoom. A woman {outfit} walks down the sidewalk, and a man walking the other way turns his "
        "head to look at her as she passes. Everyday city street, realistic phone-camera feel, she "
        "is unaware of the camera, render NO on-screen text. {characteristics}.",
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
