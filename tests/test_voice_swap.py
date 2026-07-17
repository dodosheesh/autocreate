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


def test_interlocuteur_sans_profile_accepte(tmp_path, monkeypatch):
    # [M]/[W] gardent la voix Seedance → aucun voice_profile requis pour eux.
    # On vérifie que le check des profils passe (on stoppe juste avant FFmpeg).
    def _stop(*args, **kwargs):
        raise RuntimeError("check-passe")

    monkeypatch.setattr(
        "app.services.voice_swap.audio.build_extract_audio_command", _stop
    )
    with pytest.raises(RuntimeError, match="check-passe"):
        swap_voices(
            "in.mp4",
            "out.mp4",
            "[M] tu viens d'où ?\n[F] de Paris",
            voice_map={"F": "voice_f"},
            workdir=str(tmp_path),
        )


def test_script_100_pourcent_interlocuteur_passe_tel_quel(tmp_path):
    # Aucune ligne de la model → rien à swapper : la vidéo est copiée telle quelle.
    src = tmp_path / "in.mp4"
    src.write_bytes(b"video-seedance")
    out = tmp_path / "out.mp4"
    result = swap_voices(
        str(src), str(out), "[M] question\n[W] réponse", voice_map={}, workdir=str(tmp_path)
    )
    assert result == str(out)
    assert out.read_bytes() == b"video-seedance"
