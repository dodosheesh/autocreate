"""Sonde ffprobe + normalisation des vidéos de référence copypaste.

Seedance impose à la vidéo de référence :
- durée ≤ 15 s (« The total duration of the video cannot exceed 15 seconds ») ;
- frame rate entre 23,8 et 60 FPS (« Frame rate must be between 23.8 FPS and
  60 FPS ») — fréquent sur les vidéos TikTok/Snap re-téléchargées (VFR, 20 fps…).

On sonde donc durée + fps AVANT d'envoyer à kie.ai, et on re-encode
automatiquement à 30 fps constants les vidéos hors plage. Best-effort : toute
erreur de sonde renvoie des valeurs None — on laisse alors kie.ai trancher.
"""

import json
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.integrations import r2
from app.net import UnsafeUrlError, safe_download, validate_public_url

# Contraintes Seedance 2 sur la vidéo de référence.
SEEDANCE_MIN_FPS = 23.8
SEEDANCE_MAX_FPS = 60.0
TARGET_FPS = 30


@dataclass(frozen=True)
class VideoInfo:
    duration_s: float | None = None
    fps: float | None = None


def fps_out_of_range(fps: float | None) -> bool:
    """True si le fps est CONNU et hors de la plage acceptée par Seedance."""
    return fps is not None and not (SEEDANCE_MIN_FPS <= fps <= SEEDANCE_MAX_FPS)


def _parse_rate(value: str | None) -> float | None:
    """avg_frame_rate ffprobe : « 30000/1001 » → 29.97, « 0/0 » → None."""
    if not value:
        return None
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            if float(den) == 0:
                return None
            return float(num) / float(den)
        return float(value)
    except (ValueError, ZeroDivisionError):
        return None


def probe_video_info(url_or_path: str) -> VideoInfo:
    """Durée + fps via ffprobe (lit seulement les en-têtes, pas de download
    complet). VideoInfo(None, None) si indéterminable."""
    if url_or_path.startswith(("http://", "https://")):
        try:
            validate_public_url(url_or_path)  # anti-SSRF avant de lancer ffprobe
        except UnsafeUrlError:
            return VideoInfo()
    cmd = [
        get_settings().ffprobe_bin,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate",
        "-show_entries", "format=duration",
        "-of", "json",
        url_or_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return VideoInfo()
    if proc.returncode != 0:
        return VideoInfo()
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return VideoInfo()
    duration = None
    try:
        duration = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        pass
    streams = data.get("streams") or []
    fps = _parse_rate(streams[0].get("avg_frame_rate")) if streams else None
    return VideoInfo(duration_s=duration, fps=fps)


def probe_video_duration(url_or_path: str) -> float | None:
    """Compat : durée seule."""
    return probe_video_info(url_or_path).duration_s


def normalize_reference_video(url: str, tenant_id: str) -> tuple[str, VideoInfo]:
    """Re-encode une vidéo de référence à 30 fps constants (H.264/AAC) et
    l'upload sur R2. Renvoie (nouvelle URL, infos de la vidéo normalisée).
    Lève RuntimeError si FFmpeg échoue."""
    settings = get_settings()
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src.mp4"
        safe_download(url, src)
        out = Path(tmp) / "normalized.mp4"
        cmd = [
            settings.ffmpeg_bin, "-y",
            "-i", str(src),
            "-vf", f"fps={TARGET_FPS}",
            "-r", str(TARGET_FPS),
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(
                f"FFmpeg normalisation a échoué ({proc.returncode}) : {proc.stderr[-1000:]}"
            )
        info = probe_video_info(str(out))
        key = f"refvideos/{tenant_id}/{uuid.uuid4().hex}.mp4"
        new_url = r2.upload_file(str(out), key, content_type="video/mp4")
    return new_url, VideoInfo(duration_s=info.duration_s, fps=info.fps or float(TARGET_FPS))


def strip_reference_video_audio(url: str, tenant_id: str) -> tuple[str, VideoInfo]:
    """Crée une copie silencieuse d'une référence Copypaste sur R2.

    La vidéo est réencodée pour que l'absence de piste audio soit garantie,
    y compris lorsque le conteneur source a plusieurs pistes.
    """
    settings = get_settings()
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src.mp4"
        safe_download(url, src)
        out = Path(tmp) / "silent.mp4"
        cmd = [
            settings.ffmpeg_bin, "-y",
            "-i", str(src),
            "-map", "0:v:0",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-an", "-movflags", "+faststart",
            str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(
                f"FFmpeg suppression audio a échoué ({proc.returncode}) : {proc.stderr[-1000:]}"
            )
        info = probe_video_info(str(out))
        key = f"refvideos/{tenant_id}/{uuid.uuid4().hex}-silent.mp4"
        new_url = r2.upload_file(str(out), key, content_type="video/mp4")
    return new_url, info


def downscale_reference_video(url: str, tenant_id: str) -> tuple[str, VideoInfo]:
    """Crée une référence Copypaste compatible, plafonnée à 1920×1080.

    Le ratio est conservé et aucune petite vidéo n'est agrandie. L'audio est
    conservé : cette réparation vise uniquement le refus de nombre de pixels.
    """
    settings = get_settings()
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src.mp4"
        safe_download(url, src)
        out = Path(tmp) / "1080p.mp4"
        cmd = [
            settings.ffmpeg_bin, "-y",
            "-i", str(src),
            "-map", "0:v:0", "-map", "0:a?",
            "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(
                f"FFmpeg réduction 1080p a échoué ({proc.returncode}) : {proc.stderr[-1000:]}"
            )
        info = probe_video_info(str(out))
        key = f"refvideos/{tenant_id}/{uuid.uuid4().hex}-1080p.mp4"
        new_url = r2.upload_file(str(out), key, content_type="video/mp4")
    return new_url, info
