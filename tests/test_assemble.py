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
