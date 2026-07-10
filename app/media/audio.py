"""Traitement audio du pipeline voix (brief §7.2).

Étapes couvertes ici (logique + commandes FFmpeg, testables sans réseau) :
- démux de la piste audio d'une vidéo
- détection des blocs de parole (VAD par énergie RMS, zéro dépendance lourde)
- appariement blocs ↔ lignes du script taggé (méthode « VAD + ordre »)
- découpe d'un segment, ajustement d'une conversion à la durée exacte du slot
- reconstruction de la timeline : les segments convertis remplacent les
  originaux, les silences/ambiances entre eux sont conservés tels quels
- remux de l'audio final avec la vidéo (stream video copié)
"""

import wave
from dataclasses import dataclass

import numpy as np

from app.config import get_settings


class VoiceSegmentationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Segment:
    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


# ---------- lecture / VAD ----------


def read_wav_mono(path: str) -> tuple[np.ndarray, int]:
    """WAV PCM 16-bit → float32 [-1, 1] mono."""
    with wave.open(path, "rb") as wav:
        sr = wav.getframerate()
        n_channels = wav.getnchannels()
        raw = wav.readframes(wav.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)
    return samples, sr


def detect_speech_segments(
    samples: np.ndarray,
    sr: int,
    frame_ms: int = 30,
    min_silence_s: float | None = None,
    min_speech_s: float | None = None,
    threshold_ratio: float = 0.15,
) -> list[Segment]:
    """VAD par énergie : frames de `frame_ms`, seuil relatif au pic RMS,
    fusion des blocs séparés par moins de `min_silence_s`, rejet des blocs
    plus courts que `min_speech_s`."""
    settings = get_settings()
    min_silence_s = settings.vad_min_silence_s if min_silence_s is None else min_silence_s
    min_speech_s = settings.vad_min_speech_s if min_speech_s is None else min_speech_s

    frame_len = max(int(sr * frame_ms / 1000), 1)
    n_frames = len(samples) // frame_len
    if n_frames == 0:
        return []
    frames = samples[: n_frames * frame_len].reshape(n_frames, frame_len)
    rms = np.sqrt((frames**2).mean(axis=1))
    peak = float(rms.max())
    if peak <= 1e-6:
        return []
    voiced = rms > peak * threshold_ratio

    segments: list[Segment] = []
    start = None
    for i, is_voiced in enumerate(voiced):
        t = i * frame_len / sr
        if is_voiced and start is None:
            start = t
        elif not is_voiced and start is not None:
            segments.append(Segment(start, t))
            start = None
    if start is not None:
        segments.append(Segment(start, n_frames * frame_len / sr))

    # fusion des blocs proches
    merged: list[Segment] = []
    for seg in segments:
        if merged and seg.start_s - merged[-1].end_s < min_silence_s:
            merged[-1] = Segment(merged[-1].start_s, seg.end_s)
        else:
            merged.append(seg)
    return [s for s in merged if s.duration_s >= min_speech_s]


def match_segments_to_lines(segments: list[Segment], n_lines: int) -> list[Segment]:
    """Méthode « VAD + ordre » : le segment i correspond à la ligne i du script.

    Plus de blocs que de lignes → fusion itérative des deux blocs séparés par
    le plus petit silence (une respiration a coupé une ligne en deux).
    Moins de blocs que de lignes → erreur explicite (mieux vaut rejeter l'item
    que de swapper la mauvaise voix sur la mauvaise ligne)."""
    if len(segments) < n_lines:
        raise VoiceSegmentationError(
            f"{len(segments)} bloc(s) de parole détecté(s) pour {n_lines} ligne(s) "
            "de script — segmentation impossible en méthode VAD + ordre."
        )
    segments = list(segments)
    while len(segments) > n_lines:
        gaps = [
            (segments[i + 1].start_s - segments[i].end_s, i) for i in range(len(segments) - 1)
        ]
        _, idx = min(gaps)
        segments[idx : idx + 2] = [Segment(segments[idx].start_s, segments[idx + 1].end_s)]
    return segments


# ---------- commandes FFmpeg (construites séparément → testables) ----------


def build_extract_audio_command(video_path: str, wav_path: str, sr: int = 44100) -> list[str]:
    return [
        get_settings().ffmpeg_bin,
        "-y",
        "-i", video_path,
        "-vn",
        "-ac", "1",
        "-ar", str(sr),
        "-c:a", "pcm_s16le",
        wav_path,
    ]


def build_cut_command(wav_path: str, segment: Segment, out_path: str) -> list[str]:
    return [
        get_settings().ffmpeg_bin,
        "-y",
        "-i", wav_path,
        "-ss", f"{segment.start_s:.3f}",
        "-to", f"{segment.end_s:.3f}",
        "-c:a", "pcm_s16le",
        out_path,
    ]


def build_fit_duration_command(in_path: str, out_path: str, duration_s: float) -> list[str]:
    """Cale une conversion ElevenLabs sur la durée exacte du slot d'origine
    (apad si un poil courte, atrim si un poil longue) → timeline intacte,
    lèvres toujours synchro."""
    return [
        get_settings().ffmpeg_bin,
        "-y",
        "-i", in_path,
        "-af", f"apad=whole_dur={duration_s:.3f},atrim=0:{duration_s:.3f}",
        "-ar", "44100",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        out_path,
    ]


def build_timeline_command(
    original_wav: str,
    replacements: list[tuple[Segment, str]],
    out_wav: str,
    total_duration_s: float,
) -> list[str]:
    """Reconstruit la piste : [ambiance 0→s1][conv 1][ambiance e1→s2][conv 2]…
    Les fichiers de remplacement doivent déjà faire exactement la durée du slot
    (build_fit_duration_command)."""
    if not replacements:
        raise ValueError("Aucun segment à remplacer")
    inputs: list[str] = []
    for _, path in replacements:
        inputs += ["-i", path]

    parts: list[str] = []
    labels: list[str] = []
    cursor = 0.0
    for idx, (seg, _) in enumerate(replacements):
        if seg.start_s > cursor + 1e-3:
            label = f"[g{idx}]"
            parts.append(
                f"[0:a]atrim={cursor:.3f}:{seg.start_s:.3f},asetpts=PTS-STARTPTS{label}"
            )
            labels.append(label)
        labels.append(f"[{idx + 1}:a]")
        cursor = seg.end_s
    if total_duration_s > cursor + 1e-3:
        parts.append(f"[0:a]atrim={cursor:.3f}:{total_duration_s:.3f},asetpts=PTS-STARTPTS[tail]")
        labels.append("[tail]")

    filter_complex = ";".join(
        parts + ["".join(labels) + f"concat=n={len(labels)}:v=0:a=1[aout]"]
    )
    return [
        get_settings().ffmpeg_bin,
        "-y",
        "-i", original_wav,
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[aout]",
        "-c:a", "pcm_s16le",
        out_wav,
    ]


def build_remux_command(video_path: str, audio_path: str, out_path: str) -> list[str]:
    """Remplace la piste audio de la vidéo (stream vidéo copié tel quel)."""
    return [
        get_settings().ffmpeg_bin,
        "-y",
        "-i", video_path,
        "-i", audio_path,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        out_path,
    ]
