"""Parsing des scripts taggés [H]/[F] (brief §7.1) et rendu prompt.

Le même bloc taggé sert à deux endroits :
1. Prompt Seedance — déroulé en langage naturel avec le shift de voix
   (rendu mécanique du texte fourni par l'utilisateur, jamais généré ici).
2. Étape voice-swap (Phase 3) — carte de segments : ligne N → segment N.
"""

import re
from dataclasses import dataclass

TAG_PATTERN = re.compile(r"^\[(?P<tag>[^\]]+)\]\s*(?P<text>.*)$")
BEAT_TAG = "beat"

# Comment décrire chaque voix dans le prompt Seedance (le timbre exact vient
# des voice_profiles ElevenLabs en Phase 3 ; ici on guide juste la génération)
VOICE_PROMPT_STYLES = {
    "H": "in a deep masculine voice",
    "F": "in a soft high-pitched feminine voice",
}


class DialogueParseError(ValueError):
    pass


@dataclass(frozen=True)
class DialogueSegment:
    tag: str  # H / F
    text: str


def parse_tagged_script(raw_text: str, valid_tags: set[str] | None = None) -> list[DialogueSegment]:
    """Une ligne = un segment = une voix, ordre chronologique.
    Les lignes [beat] (micro-pause) ne créent pas de segment audio."""
    valid = valid_tags or set(VOICE_PROMPT_STYLES)
    segments: list[DialogueSegment] = []
    for lineno, line in enumerate(raw_text.strip().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        match = TAG_PATTERN.match(line)
        if not match:
            raise DialogueParseError(
                f"Ligne {lineno} sans tag [H]/[F]/[beat] : {line[:60]!r}"
            )
        tag, text = match.group("tag"), match.group("text").strip()
        if tag == BEAT_TAG:
            continue
        if tag not in valid:
            raise DialogueParseError(f"Ligne {lineno} : tag inconnu [{tag}]")
        if not text:
            raise DialogueParseError(f"Ligne {lineno} : tag [{tag}] sans texte")
        segments.append(DialogueSegment(tag=tag, text=text))
    if not segments:
        raise DialogueParseError("Script vide : aucune ligne parlée")
    return segments


def render_for_prompt(raw_text: str) -> str:
    """Déroulé naturel pour le prompt Seedance, avec micro-pause aux [beat]."""
    parts: list[str] = []
    first = True
    pending_beat = False
    for line in raw_text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        match = TAG_PATTERN.match(line)
        if match and match.group("tag") == BEAT_TAG:
            pending_beat = True
            continue
        segment = parse_tagged_script(line)[0]
        style = VOICE_PROMPT_STYLES[segment.tag]
        if first:
            parts.append(f'She says {style}: "{segment.text}"')
            first = False
        else:
            transition = "after a brief pause, " if pending_beat else ""
            parts.append(f'then {transition}{style}: "{segment.text}"')
        pending_beat = False
    return ", ".join(parts) + "."
