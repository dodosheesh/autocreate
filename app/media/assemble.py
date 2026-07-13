"""Assemblage FFmpeg (brief §10).

Normalise la vidéo (déjà voix-swappée le cas échéant) au format de livraison :
- 9:16 exact (scale + crop centré, pas de bandes noires)
- résolution et bitrate du job (presets standard/high)
- overlay barre de texte façon Snapchat (bande semi-transparente pleine
  largeur + texte centré, contenu depuis le slot {caption})
- piste musique optionnelle mixée sous la voix (music_url du job)
- audio AAC, faststart pour le streaming

Le texte du caption passe par un fichier (drawtext textfile=) pour éviter
tout problème d'échappement — le contenu vient de l'utilisateur.
"""

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings

# Emojis / pictogrammes : la police vidéo (DejaVu) ne les rend pas → carrés moches.
# On les retire du bandeau (drawtext ne sait pas afficher les emojis couleur).
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U0000FE00-\U0000FE0F\U00002B00-\U00002BFF\U0000200D\U00002300-\U000023FF]+",
    flags=re.UNICODE,
)


def _clean_caption(text: str) -> str:
    """Retire les emojis et normalise les espaces (drawtext ne gère pas l'emoji)."""
    return " ".join(_EMOJI_RE.sub("", text).split())


def _wrap_caption(text: str, max_chars: int = 26, max_lines: int = 4) -> list[str]:
    """Découpe le caption en lignes (par mots) pour qu'il ne déborde pas de l'écran."""
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines[:max_lines]

RESOLUTIONS: dict[str, tuple[int, int]] = {
    "480p": (480, 854),
    "720p": (720, 1280),
    "1080p": (1080, 1920),
}

# Presets bitrate vidéo (kbps) par résolution — façon Higgsfield standard/high
BITRATES_KBPS: dict[tuple[str, str], int] = {
    ("480p", "standard"): 2500,
    ("480p", "high"): 4000,
    ("720p", "standard"): 4000,
    ("720p", "high"): 8000,
    ("1080p", "standard"): 8000,
    ("1080p", "high"): 12000,
}


@dataclass(frozen=True)
class AssembleParams:
    resolution: str = "720p"
    bitrate: str = "standard"  # standard / high
    # Un fichier texte par LIGNE du caption (retour à la ligne → pas de texte coupé).
    caption_files: tuple[str, ...] = field(default_factory=tuple)
    music_path: str | None = None  # piste musique locale à mixer sous la voix


def _snapchat_overlay(caption_files: tuple[str, ...], height: int) -> str:
    """Bande noire semi-transparente pleine largeur (tiers haut), texte blanc
    centré sur PLUSIEURS lignes — façon barre de légende Snapchat. Chaque ligne
    est un drawtext centré (évite le texte qui déborde/est coupé)."""
    n = max(1, len(caption_files))
    font_size = round(height * 0.030)
    line_h = round(font_size * 1.35)
    pad = round(font_size * 0.7)
    bar_h = n * line_h + 2 * pad
    bar_y = round(height * 0.14)
    parts = [f"drawbox=x=0:y={bar_y}:w=iw:h={bar_h}:color=black@0.55:t=fill"]
    for i, cf in enumerate(caption_files):
        y = bar_y + pad + i * line_h
        # expansion=none : le contenu utilisateur est rendu littéralement (pas
        # d'interprétation des directives %{...} → pas de fuite/erreur FFmpeg).
        parts.append(
            f"drawtext=textfile='{cf}':expansion=none:font=Sans:fontcolor=white:"
            f"fontsize={font_size}:x=(w-text_w)/2:y={y}"
        )
    return ",".join(parts)


def build_assemble_command(
    input_path: str, output_path: str, params: AssembleParams
) -> list[str]:
    """Construit la commande FFmpeg (séparé de l'exécution → testable)."""
    if params.resolution not in RESOLUTIONS:
        raise ValueError(f"Résolution inconnue : {params.resolution}")
    width, height = RESOLUTIONS[params.resolution]
    kbps = BITRATES_KBPS[(params.resolution, params.bitrate)]
    # Remplit le cadre 9:16 puis crop centré (pas de bandes noires)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1"
    )
    if params.caption_files:
        vf += "," + _snapchat_overlay(params.caption_files, height)

    cmd = [
        get_settings().ffmpeg_bin,
        "-y",
        "-i", input_path,
    ]
    if params.music_path:
        volume_db = get_settings().music_volume_db
        cmd += [
            "-stream_loop", "-1",
            "-i", params.music_path,
            "-filter_complex",
            (
                f"[0:v]{vf}[vout];"
                f"[1:a]volume={volume_db}dB[music];"
                f"[0:a][music]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            ),
            "-map", "[vout]",
            "-map", "[aout]",
        ]
    else:
        cmd += ["-vf", vf]
    cmd += [
        "-c:v", "libx264",
        "-b:v", f"{kbps}k",
        "-maxrate", f"{int(kbps * 1.2)}k",
        "-bufsize", f"{kbps * 2}k",
        "-preset", "medium",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]
    return cmd


def assemble(
    input_path: str,
    output_path: str,
    params: AssembleParams,
    caption_text: str | None = None,
    workdir: str | None = None,
) -> None:
    """Exécute l'assemblage ; écrit le caption dans un fichier temporaire
    (workdir requis si caption_text fourni)."""
    if caption_text:
        if workdir is None:
            raise ValueError("workdir requis pour un caption_text")
        lines = _wrap_caption(_clean_caption(caption_text))  # emoji retirés + wrap
        caption_files: list[str] = []
        for i, line in enumerate(lines):
            cf = str(Path(workdir) / f"caption_{i}.txt")
            Path(cf).write_text(line, encoding="utf-8")
            caption_files.append(cf)
        params = AssembleParams(
            resolution=params.resolution,
            bitrate=params.bitrate,
            caption_files=tuple(caption_files),
            music_path=params.music_path,
        )
    cmd = build_assemble_command(input_path, output_path, params)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg a échoué ({proc.returncode}) : {proc.stderr[-2000:]}")
