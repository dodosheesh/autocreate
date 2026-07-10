import pytest

from app.media.assemble import AssembleParams, build_assemble_command


def test_commande_720p_standard():
    cmd = build_assemble_command("in.mp4", "out.mp4", AssembleParams("720p", "standard"))
    joined = " ".join(cmd)
    assert "scale=720:1280" in joined
    assert "crop=720:1280" in joined
    assert "-b:v 4000k" in joined
    assert "+faststart" in joined


def test_commande_1080p_high():
    cmd = build_assemble_command("in.mp4", "out.mp4", AssembleParams("1080p", "high"))
    joined = " ".join(cmd)
    assert "scale=1080:1920" in joined
    assert "-b:v 12000k" in joined


def test_resolution_inconnue():
    with pytest.raises(ValueError):
        build_assemble_command("in.mp4", "out.mp4", AssembleParams("4k", "standard"))


def test_overlay_caption_snapchat():
    cmd = build_assemble_command(
        "in.mp4", "out.mp4", AssembleParams("720p", "standard", caption_file="cap.txt")
    )
    vf = cmd[cmd.index("-vf") + 1]
    assert "drawbox=" in vf and "color=black@0.55" in vf
    assert "drawtext=textfile='cap.txt'" in vf
    assert "fontcolor=white" in vf


def test_mix_musique():
    cmd = build_assemble_command(
        "in.mp4", "out.mp4", AssembleParams("720p", "standard", music_path="music.mp3")
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "amix=inputs=2:duration=first" in fc
    assert "volume=-18.0dB" in fc
    assert "-stream_loop" in cmd  # musique bouclée sur la durée de la vidéo
    assert "[vout]" in fc and "[aout]" in fc
