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


def build_last_frame_command(video_path: str, out_path: str) -> list[str]:
    """Commande FFmpeg qui extrait la TOUTE DERNIÈRE frame d'une vidéo en JPEG.

    Sert au format long 30 s : le clip 2 démarre visuellement depuis la dernière
    frame du clip 1 (même décor, même tenue, même cadrage) → enchaînement continu.
    `-sseof -0.2` se place 0,2 s avant la fin (la frame finale exacte est parfois
    illisible), `-update 1 -frames:v 1` n'écrit qu'une image."""
    settings = get_settings()
    return [
        settings.ffmpeg_bin,
        "-y",
        "-sseof", "-0.2",
        "-i", video_path,
        "-update", "1",
        "-frames:v", "1",
        "-q:v", "2",
        out_path,
    ]


def extract_last_frame(video_path: str, out_path: str) -> str:
    """Extrait la dernière frame de `video_path` vers `out_path` (JPEG). Renvoie
    le chemin ; lève RuntimeError si l'extraction échoue."""
    proc = subprocess.run(
        build_last_frame_command(video_path, out_path), capture_output=True, text=True
    )
    if proc.returncode != 0 or not Path(out_path).exists():
        raise RuntimeError(
            f"Extraction de la dernière frame échouée : {proc.stderr[-2000:]}"
        )
    return out_path


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
