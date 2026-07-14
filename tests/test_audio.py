import numpy as np
import pytest

from app.media.audio import (
    Segment,
    VoiceSegmentationError,
    build_fit_duration_command,
    build_remux_command,
    build_timeline_command,
    detect_speech_segments,
    match_segments_to_lines,
)

SR = 16000


def synth(*blocks: tuple[str, float]) -> np.ndarray:
    """Construit un signal synthétique : ('speech'|'silence', durée_s)."""
    parts = []
    for kind, duration in blocks:
        n = int(SR * duration)
        if kind == "speech":
            t = np.arange(n) / SR
            parts.append(0.5 * np.sin(2 * np.pi * 220 * t).astype(np.float32))
        else:
            parts.append(np.zeros(n, dtype=np.float32))
    return np.concatenate(parts)


def test_detection_deux_blocs_de_parole():
    samples = synth(("silence", 0.5), ("speech", 1.0), ("silence", 0.8), ("speech", 0.7))
    segments = detect_speech_segments(samples, SR)
    assert len(segments) == 2
    assert segments[0].start_s == pytest.approx(0.5, abs=0.1)
    assert segments[0].end_s == pytest.approx(1.5, abs=0.1)
    assert segments[1].start_s == pytest.approx(2.3, abs=0.1)


def test_respiration_courte_fusionnee():
    # pause de 0.1 s < min_silence 0.25 s → un seul segment
    samples = synth(("speech", 0.6), ("silence", 0.1), ("speech", 0.6))
    assert len(detect_speech_segments(samples, SR)) == 1


def test_silence_total_aucun_segment():
    assert detect_speech_segments(synth(("silence", 2.0)), SR) == []


def test_match_exact():
    segments = [Segment(0.0, 1.0), Segment(2.0, 3.0)]
    assert match_segments_to_lines(segments, 2) == segments


def test_match_fusionne_le_plus_petit_gap():
    segments = [Segment(0.0, 1.0), Segment(1.1, 2.0), Segment(4.0, 5.0)]
    result = match_segments_to_lines(segments, 2)
    assert result == [Segment(0.0, 2.0), Segment(4.0, 5.0)]


def test_match_moins_de_blocs_decoupe_proportionnellement():
    # parole continue (1 bloc) pour 2 lignes → 2 tranches égales (au lieu d'échouer)
    assert match_segments_to_lines([Segment(0.0, 2.0)], 2) == [
        Segment(0.0, 1.0), Segment(1.0, 2.0)
    ]


def test_match_aucun_bloc_leve_erreur():
    with pytest.raises(VoiceSegmentationError, match="Aucune parole"):
        match_segments_to_lines([], 2)


def test_timeline_command_structure():
    replacements = [
        (Segment(0.5, 1.5), "conv0.wav"),
        (Segment(2.3, 3.0), "conv1.wav"),
    ]
    cmd = build_timeline_command("full.wav", replacements, "out.wav", total_duration_s=4.0)
    fc = cmd[cmd.index("-filter_complex") + 1]
    # ambiance avant, entre et après les segments + 2 conversions = concat n=5
    assert "atrim=0.000:0.500" in fc
    assert "atrim=1.500:2.300" in fc
    assert "atrim=3.000:4.000" in fc
    assert "concat=n=5:v=0:a=1[aout]" in fc
    assert cmd.count("-i") == 3  # original + 2 conversions


def test_timeline_segment_au_debut_sans_gap_initial():
    cmd = build_timeline_command(
        "full.wav", [(Segment(0.0, 1.0), "conv0.wav")], "out.wav", total_duration_s=2.0
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "atrim=0.000:0.000" not in fc
    assert "concat=n=2:v=0:a=1[aout]" in fc


def test_fit_duration_pad_et_trim():
    cmd = build_fit_duration_command("in.mp3", "out.wav", 1.234)
    af = cmd[cmd.index("-af") + 1]
    assert af == "apad=whole_dur=1.234,atrim=0:1.234"


def test_remux_copie_la_video():
    cmd = build_remux_command("video.mp4", "audio.wav", "out.mp4")
    joined = " ".join(cmd)
    assert "-map 0:v:0" in joined and "-map 1:a:0" in joined
    assert "-c:v copy" in joined
