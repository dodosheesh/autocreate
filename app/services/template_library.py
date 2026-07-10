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
DEFAULT_TEMPLATES: list[tuple[str, str, bool]] = [
    # ---------------- skit (sketch, comédie de situation) ----------------
    (
        "skit",
        "Vertical 9:16 comedic skit. A young woman {outfit} in {background}. "
        "Handheld camera, natural daylight, energetic mid-shot that snaps to a "
        "close-up on the punchline. Expressive face, quick comedic timing, {characteristics}. "
        "{dialogue}",
        True,
    ),
    (
        "skit",
        "Vertical skit, single continuous take. She {outfit} reacts to something "
        "off-screen in {background}, exaggerated facial expressions and hand gestures, "
        "phone-style handheld framing, bright even lighting. {characteristics}. {dialogue}",
        True,
    ),
    (
        "skit",
        "POV comedic bit, 9:16. A woman {outfit} talks straight to camera as if to a "
        "friend, {background} behind her slightly out of focus, playful head tilts and "
        "eyebrow raises, warm indoor light. {characteristics}. {dialogue}",
        True,
    ),
    # ---------------- storytelling (récit face caméra, vlog) ----------------
    (
        "storytelling",
        "Cinematic vertical vlog. A young woman {outfit} sits in {background}, speaking "
        "intimately to camera. Soft window light, shallow depth of field, subtle handheld "
        "drift, occasional slow push-in for emphasis. Expressive, sincere delivery, "
        "{characteristics}. {dialogue}",
        True,
    ),
    (
        "storytelling",
        "Storytime to camera, 9:16, golden-hour mood. She {outfit} in {background}, gentle "
        "hair movement, cozy cinematic grade, catchlights in the eyes. She recounts a "
        "personal story with natural gestures. {characteristics}. {dialogue}",
        True,
    ),
    (
        "storytelling",
        "Confessional-style vertical clip. Close, intimate framing of a woman {outfit} in "
        "{background}, dim moody key light with a soft rim, calm and reflective tone, small "
        "authentic micro-expressions. {characteristics}. {dialogue}",
        True,
    ),
    # ---------------- showing_body (mode / présentation, peu ou pas parlé) ----------------
    (
        "showing_body",
        "Vertical fashion clip. A woman {outfit} poses and turns slowly in {background}, "
        "full-body to mid framing, smooth slow camera orbit, flattering soft key light, "
        "confident runway energy, subtle slow motion. {characteristics}.",
        False,
    ),
    (
        "showing_body",
        "9:16 outfit showcase. She {outfit} walks toward camera in {background}, then a "
        "detail pan over the outfit, crisp editorial lighting, glossy fashion-film look, "
        "poised expression. {characteristics}.",
        False,
    ),
    (
        "showing_body",
        "Mirror-selfie style vertical clip, phone in hand. A woman {outfit} in {background} "
        "checks her look with playful poses, bright soft light, casual confident vibe. "
        "{characteristics}.",
        False,
    ),
    # ---------------- micro_trottoir (interview de rue) ----------------
    (
        "micro_trottoir",
        "Street interview, vertical 9:16 handheld. A woman {outfit} answers an off-screen "
        "interviewer in {background} (busy street), natural ambient light, candid documentary "
        "framing, quick reactive expressions as she replies. {characteristics}. {dialogue}",
        True,
    ),
    (
        "micro_trottoir",
        "Vox-pop on the go, 9:16. She {outfit} is stopped in {background}, mic just out of "
        "frame, real-world lighting, spontaneous laughter and gestures between answers. "
        "{characteristics}. {dialogue}",
        True,
    ),
    (
        "micro_trottoir",
        "Man-on-the-street clip. A woman {outfit} reacts to a surprising question in "
        "{background}, handheld follow, candid crowd behind her, natural daylight, genuine "
        "spontaneous delivery. {characteristics}. {dialogue}",
        True,
    ),
    # ---------------- podcast (plateau, elle parle au micro) ----------------
    (
        "podcast",
        "Podcast set, vertical 9:16. A woman {outfit} sits at a mic in {background} (studio "
        "with warm accent lighting), relaxed conversational body language, cinematic bokeh, "
        "she leans in on key points. {characteristics}. {dialogue}",
        True,
    ),
    (
        "podcast",
        "Vertical podcast clip, single-cam framing. She {outfit} talks into a studio mic in "
        "{background}, moody colored key light, natural gestures, expressive eye contact with "
        "the camera. {characteristics}. {dialogue}",
        True,
    ),
    (
        "podcast",
        "Hot-take podcast moment, 9:16. A woman {outfit} in {background} delivers an opinion "
        "with animated hands, punchy studio lighting, subtle push-in on the strong line. "
        "{characteristics}. {dialogue}",
        True,
    ),
    # ---------------- snapchat (situation sociale candide + barre de légende) ----------------
    (
        "snapchat",
        "Candid vertical clip, phone-filmed look. A woman {outfit} in {background} (public "
        "social setting) unaware she's being filmed, natural available light, slightly raw "
        "handheld framing, a Snapchat-style caption bar overlays the top. Caption: {caption}. "
        "{characteristics}.",
        False,
    ),
    (
        "snapchat",
        "Sneaky candid 9:16 in {background}. She {outfit} goes about a mundane moment with a "
        "quirky offbeat behavior, realistic phone-camera grain, Snapchat text bar at the top. "
        "Caption: {caption}. {characteristics}.",
        False,
    ),
    (
        "snapchat",
        "Filmed-from-across-the-room vibe, vertical. A woman {outfit} in {background} caught in "
        "a funny everyday situation, natural light, authentic candid energy, Snapchat caption "
        "overlay up top. Caption: {caption}. {characteristics}.",
        False,
    ),
]


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
