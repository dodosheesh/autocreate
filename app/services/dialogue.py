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

# Chaque tag = un LOCUTEUR (sujet) + un timbre décrit dans le prompt Seedance.
# H / F = LA MODEL elle-même (elle baisse la voix, puis voix féminine) → « she ».
# M / W = une AUTRE personne (ex. l'intervieweur en micro-trottoir) → « a man »
#         / « another woman » : le prompt sait ainsi qui dit quoi.
# Le timbre exact vient des voice_profiles ElevenLabs (Phase 3) : crée un profil
# par tag utilisé (H, F, M, W…) pour le voice-swap.
SPEAKERS: dict[str, tuple[str, str]] = {
    "H": ("she", "in a deep masculine voice"),
    "F": ("she", "in a soft high-pitched feminine voice"),
    "M": ("a man off-camera", "in a natural male voice"),
    "W": ("another woman", "in a natural female voice"),
}

# Rétro-compat : timbre seul par tag (dérivé de SPEAKERS).
VOICE_PROMPT_STYLES = {tag: style for tag, (_subject, style) in SPEAKERS.items()}

# Normalisation : accepte les tags « à la suite » sur une même ligne (ex.
# « [F] salut [H] bro ») en remettant chaque tag CONNU en début de ligne.
_KNOWN_TAGS_ALT = "|".join(list(SPEAKERS) + [BEAT_TAG])
_NORMALIZE_RE = re.compile(rf"\s*(\[(?:{_KNOWN_TAGS_ALT})\])")


def _normalize_lines(raw_text: str) -> str:
    """Une ligne par réplique, que les tags soient à la ligne OU à la suite."""
    return _NORMALIZE_RE.sub(r"\n\1", raw_text).strip()


class DialogueParseError(ValueError):
    pass


@dataclass(frozen=True)
class DialogueSegment:
    tag: str  # H / F
    text: str


def parse_tagged_script(raw_text: str, valid_tags: set[str] | None = None) -> list[DialogueSegment]:
    """Une ligne = un segment = une voix, ordre chronologique.
    Les lignes [beat] (micro-pause) ne créent pas de segment audio."""
    valid = valid_tags or set(SPEAKERS)
    segments: list[DialogueSegment] = []
    for lineno, line in enumerate(_normalize_lines(raw_text).splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        match = TAG_PATTERN.match(line)
        if not match:
            raise DialogueParseError(
                f"Ligne {lineno} sans tag [H]/[F]/[M]/[W]/[beat] : {line[:60]!r}"
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
    """Déroulé naturel pour le prompt Seedance, avec micro-pause aux [beat].

    Attribue explicitement chaque réplique à son locuteur : la model (« she »,
    tags H/F) vs une autre personne (« a man off-camera », tags M/W). Quand le
    locuteur ne change pas, on n'répète pas le sujet (lecture fluide)."""
    parts: list[str] = []
    first = True
    pending_beat = False
    prev_subject = None
    for line in _normalize_lines(raw_text).splitlines():
        line = line.strip()
        if not line:
            continue
        match = TAG_PATTERN.match(line)
        if match and match.group("tag") == BEAT_TAG:
            pending_beat = True
            continue
        segment = parse_tagged_script(line)[0]
        subject, style = SPEAKERS[segment.tag]
        transition = "after a brief pause, " if pending_beat else ""
        if first:
            parts.append(f'{subject.capitalize()} says {style}: "{segment.text}"')
        elif subject == prev_subject:  # même locuteur → pas de répétition du sujet
            parts.append(f'then {transition}{style}: "{segment.text}"')
        else:  # changement de personne → on le nomme (qui dit quoi)
            parts.append(f'then {subject} says {transition}{style}: "{segment.text}"')
        first = False
        prev_subject = subject
        pending_beat = False
    return ", ".join(parts) + "."
