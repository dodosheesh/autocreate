"""Sonde ffprobe (durée d'une vidéo distante ou locale).

Sert à valider les vidéos de référence copypaste AVANT d'envoyer à kie.ai :
Seedance refuse les vidéos > 15 s avec une 422 opaque, on préfère bloquer côté
app avec un message clair. Best-effort : toute erreur (URL non publique, DNS,
ffprobe manquant, timeout) renvoie None — on laisse alors kie.ai trancher.
"""

import subprocess

from app.config import get_settings
from app.net import UnsafeUrlError, validate_public_url


def probe_video_duration(url_or_path: str) -> float | None:
    """Durée en secondes via ffprobe (lit seulement les en-têtes du fichier,
    pas de téléchargement complet). None si indéterminable."""
    if url_or_path.startswith(("http://", "https://")):
        try:
            validate_public_url(url_or_path)  # anti-SSRF avant de lancer ffprobe
        except UnsafeUrlError:
            return None
    cmd = [
        get_settings().ffprobe_bin,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        url_or_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None
