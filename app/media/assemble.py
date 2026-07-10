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

import subprocess
from dataclasses import dataclass
from pathlib import Path

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
    caption_file: str | None = None  # fichier texte du caption (overlay Snapchat)
    music_path: str | None = None  # piste musique locale à mixer sous la voix


def _snapchat_overlay(caption_file: str, height: int) -> str:
    """Bande noire semi-transparente pleine largeur dans le tiers haut,
    texte blanc centré — façon barre de légende Snapchat."""
    bar_y = round(height * 0.16)
    bar_h = round(height * 0.075)
    font_size = round(height * 0.032)
    # expansion=none : le contenu du caption (fourni par l'utilisateur) est
    # rendu littéralement, sans que drawtext interprète les directives %{...}
    # (sinon fuite de métadonnées de frame / erreur FFmpeg via un caption piégé).
    return (
        f"drawbox=x=0:y={bar_y}:w=iw:h={bar_h}:color=black@0.55:t=fill,"
        f"drawtext=textfile='{caption_file}':expansion=none:font=Sans:fontcolor=white:"
        f"fontsize={font_size}:x=(w-text_w)/2:y={bar_y}+({bar_h}-text_h)/2"
    )


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
    if params.caption_file:
        vf += "," + _snapchat_overlay(params.caption_file, height)

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
        caption_file = str(Path(workdir) / "caption.txt")
        Path(caption_file).write_text(caption_text, encoding="utf-8")
        params = AssembleParams(
            resolution=params.resolution,
            bitrate=params.bitrate,
            caption_file=caption_file,
            music_path=params.music_path,
        )
    cmd = build_assemble_command(input_path, output_path, params)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg a échoué ({proc.returncode}) : {proc.stderr[-2000:]}")
