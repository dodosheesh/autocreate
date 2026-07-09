"""Assemblage FFmpeg (brief §10) — Phase 1.

Normalise la vidéo brute Seedance au format de livraison :
- 9:16 exact (scale + crop centré si nécessaire)
- résolution et bitrate du job (presets standard/high)
- audio AAC, faststart pour le streaming

Phase 3+ ajoutera : mix musique (music_url), overlay barre Snapchat
(drawtext depuis le slot {caption}), sous-titres, remux audio voix-swappé.
"""

import subprocess
from dataclasses import dataclass

from app.config import get_settings

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
    return [
        get_settings().ffmpeg_bin,
        "-y",
        "-i", input_path,
        "-vf", vf,
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


def assemble(input_path: str, output_path: str, params: AssembleParams) -> None:
    cmd = build_assemble_command(input_path, output_path, params)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg a échoué ({proc.returncode}) : {proc.stderr[-2000:]}")
