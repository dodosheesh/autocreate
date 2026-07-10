"""Extraction d'images-clés d'une vidéo (reverse-engineering vidéo).

On échantillonne N frames réparties uniformément sur la durée pour donner au
LLM vision un aperçu chronologique du plan (scène, action, cadrage, ambiance).
"""

import subprocess
from pathlib import Path

from app.config import get_settings


def _probe_duration_s(video_path: str) -> float:
    settings = get_settings()
    proc = subprocess.run(
        [
            settings.ffprobe_bin,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(proc.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def extract_keyframes(video_path: str, out_dir: str, n: int = 6) -> list[str]:
    """Extrait `n` frames JPEG réparties dans la vidéo, renvoie leurs chemins.

    Utilise l'instant milieu de chaque tranche (évite la toute première/dernière
    frame souvent noire). Chaque frame est extraite par un seek rapide (-ss).
    """
    settings = get_settings()
    n = max(1, n)
    duration = _probe_duration_s(video_path)
    out = Path(out_dir)
    paths: list[str] = []
    for i in range(n):
        # instant = milieu de la i-ème tranche ; fallback 0 si durée inconnue
        ts = (duration * (i + 0.5) / n) if duration > 0 else 0.0
        frame_path = out / f"frame_{i:02d}.jpg"
        proc = subprocess.run(
            [
                settings.ffmpeg_bin,
                "-y",
                "-ss", f"{ts:.3f}",
                "-i", video_path,
                "-frames:v", "1",
                "-q:v", "3",
                str(frame_path),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and frame_path.exists():
            paths.append(str(frame_path))
    if not paths:
        raise RuntimeError("Aucune frame extraite de la vidéo")
    return paths
