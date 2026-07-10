"""Orchestration du voice-swap (brief §7.2) :

    vidéo brute (audio Seedance) ─► démux ─► VAD ─► appariement lignes
        ─► speech_to_speech par segment (voice_id du tag [H]/[F])
        ─► ajustement durée exacte ─► reconstruction timeline ─► remux

Conversions same-gender uniquement : Seedance voice déjà chaque ligne dans
le bon genre, le tag ne fait que choisir le timbre cible fixe.
"""

import subprocess
from pathlib import Path

from app.integrations.elevenlabs import speech_to_speech
from app.media import audio
from app.services.dialogue import parse_tagged_script


class VoiceSwapError(RuntimeError):
    pass


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise VoiceSwapError(f"FFmpeg a échoué ({proc.returncode}) : {proc.stderr[-2000:]}")


def swap_voices(
    video_in: str,
    video_out: str,
    dialogue_script: str,
    voice_map: dict[str, str],
    workdir: str,
) -> str:
    """Remplace le timbre de chaque ligne parlée selon son tag.

    voice_map : tag → elevenlabs_voice_id (depuis la table voice_profiles).
    """
    lines = parse_tagged_script(dialogue_script)
    missing = {line.tag for line in lines} - set(voice_map)
    if missing:
        raise VoiceSwapError(
            f"Aucun voice_profile pour le(s) tag(s) {sorted(missing)} — "
            "créer les profils via /api/banks/voices."
        )

    work = Path(workdir)
    full_wav = str(work / "audio_full.wav")
    _run(audio.build_extract_audio_command(video_in, full_wav))

    samples, sr = audio.read_wav_mono(full_wav)
    total_duration_s = len(samples) / sr
    segments = audio.detect_speech_segments(samples, sr)
    segments = audio.match_segments_to_lines(segments, len(lines))

    replacements: list[tuple[audio.Segment, str]] = []
    for i, (segment, line) in enumerate(zip(segments, lines)):
        raw_seg = str(work / f"seg_{i}.wav")
        converted = str(work / f"seg_{i}_converted.mp3")
        fitted = str(work / f"seg_{i}_fitted.wav")
        _run(audio.build_cut_command(full_wav, segment, raw_seg))
        speech_to_speech(raw_seg, voice_map[line.tag], converted)
        _run(audio.build_fit_duration_command(converted, fitted, segment.duration_s))
        replacements.append((segment, fitted))

    swapped_wav = str(work / "audio_swapped.wav")
    _run(
        audio.build_timeline_command(full_wav, replacements, swapped_wav, total_duration_s)
    )
    _run(audio.build_remux_command(video_in, swapped_wav, video_out))
    return video_out
