import pytest

from app.services.voice_swap import VoiceSwapError, swap_voices


def test_tag_sans_voice_profile_rejette_avant_tout_traitement(tmp_path):
    # Le check du voice_map arrive avant tout appel FFmpeg/ElevenLabs
    with pytest.raises(VoiceSwapError, match=r"\['F'\]"):
        swap_voices(
            "in.mp4",
            "out.mp4",
            "[H] ligne une\n[F] ligne deux",
            voice_map={"H": "voice_h"},
            workdir=str(tmp_path),
        )
