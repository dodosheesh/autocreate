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
# [action]/[do] = ce que la model FAIT (mise en scène), PAS ce qu'elle dit :
# rendu comme une action dans le prompt, jamais parlé ni voice-swappé.
ACTION_TAGS = {"action", "do"}

_KNOWN_TAGS_ALT = "|".join(list(SPEAKERS) + [BEAT_TAG] + sorted(ACTION_TAGS))
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
                f"Ligne {lineno} sans tag [H]/[F]/[M]/[W]/[action]/[beat] : {line[:60]!r}"
            )
        tag, text = match.group("tag"), match.group("text").strip()
        # [beat] (pause) et [action]/[do] (mise en scène) ne créent pas de segment
        # audio : ils ne sont ni parlés ni voice-swappés.
        if tag == BEAT_TAG or tag in ACTION_TAGS:
            continue
        if tag not in valid:
            raise DialogueParseError(f"Ligne {lineno} : tag inconnu [{tag}]")
        if not text:
            raise DialogueParseError(f"Ligne {lineno} : tag [{tag}] sans texte")
        segments.append(DialogueSegment(tag=tag, text=text))
    if not segments:
        raise DialogueParseError("Script vide : aucune ligne parlée")
    return segments


def _line_word_count(line: str) -> int:
    """Nb de mots PARLÉS d'une ligne (0 pour [beat] / [action] — non parlés)."""
    m = TAG_PATTERN.match(line)
    if not m:
        return 0
    tag = m.group("tag")
    if tag == BEAT_TAG or tag in ACTION_TAGS:
        return 0
    return len(m.group("text").split())


def split_script_halves(raw_text: str) -> tuple[str, str]:
    """Découpe un script taggé en 2 moitiés équilibrées par nombre de mots
    parlés (pour 2 clips, ex. 2×15 s). Ne coupe jamais au milieu d'une réplique.
    Renvoie (première_moitié, seconde_moitié) en texte taggé."""
    lines = [ln.strip() for ln in _normalize_lines(raw_text).splitlines() if ln.strip()]
    total = sum(_line_word_count(ln) for ln in lines)
    if total == 0 or len(lines) <= 1:
        return "\n".join(lines), ""
    acc, split_idx = 0, len(lines)
    for i, ln in enumerate(lines):
        acc += _line_word_count(ln)
        if acc >= total / 2 and i + 1 < len(lines):
            split_idx = i + 1
            break
    return "\n".join(lines[:split_idx]), "\n".join(lines[split_idx:])


def render_for_prompt(raw_text: str) -> str:
    """Déroulé naturel pour le prompt Seedance.

    - [H]/[F]/[M]/[W] : PAROLE, attribuée à son locuteur (la model « she » vs une
      autre personne). Locuteur inchangé → on ne répète pas le sujet.
    - [action]/[do] : ce qu'elle FAIT (mise en scène) — décrit comme une action,
      jamais entre guillemets, jamais parlé.
    - [beat] : micro-pause avant la réplique suivante."""
    frags: list[str] = []
    pending_beat = False
    prev_subject = None
    for line in _normalize_lines(raw_text).splitlines():
        line = line.strip()
        if not line:
            continue
        match = TAG_PATTERN.match(line)
        if not match:
            continue
        tag, text = match.group("tag"), match.group("text").strip()
        if tag == BEAT_TAG:
            pending_beat = True
            continue
        transition = "after a brief pause, " if pending_beat else ""
        pending_beat = False
        if tag in ACTION_TAGS:  # une ACTION (non parlée)
            frags.append(f"{transition}she {text}".strip())
            prev_subject = None  # une action re-attribuera la parole suivante
            continue
        subject, style = SPEAKERS.get(tag, ("she", ""))
        if not frags:
            frags.append(f'{subject} says {style}: "{text}"')
        elif subject == prev_subject:  # même locuteur → pas de répétition du sujet
            frags.append(f'{transition}{style}: "{text}"')
        else:  # changement de personne (ou après une action) → on nomme qui parle
            frags.append(f'{subject} says {transition}{style}: "{text}"')
        prev_subject = subject
    if not frags:
        return ""
    frags[0] = frags[0][0].upper() + frags[0][1:]
    return ", then ".join(frags) + "."
