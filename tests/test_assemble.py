import pytest

from app.media.assemble import (
    AssembleParams,
    _clean_caption,
    _wrap_caption,
    build_assemble_command,
    build_concat_command,
)


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


def test_overlay_caption_snapchat_multiligne():
    cmd = build_assemble_command(
        "in.mp4", "out.mp4",
        AssembleParams("720p", "standard", caption_files=("l0.txt", "l1.txt")),
    )
    vf = cmd[cmd.index("-vf") + 1]
    # barre PLEINE LARGEUR collée aux deux bords (x=0, w=iw) façon Snapchat
    assert "drawbox=x=0:" in vf and "w=iw" in vf and "color=black@0.5" in vf
    # une ligne = un drawtext centré (texte qui passe à la ligne, jamais coupé)
    assert "drawtext=textfile='l0.txt'" in vf and "drawtext=textfile='l1.txt'" in vf
    assert vf.count("drawtext=") == 2
    assert "fontcolor=white" in vf


def test_concat_deux_clips():
    cmd = build_concat_command(["a.mp4", "b.mp4"], "out.mp4")
    joined = " ".join(cmd)
    assert "-i a.mp4" in joined and "-i b.mp4" in joined
    assert "concat=n=2:v=1:a=1[v][a]" in joined
    assert "[0:v][0:a][1:v][1:a]" in joined
    assert cmd[-1] == "out.mp4"


def test_concat_exige_deux_clips():
    with pytest.raises(ValueError):
        build_concat_command(["only.mp4"], "out.mp4")


def test_clean_caption_retire_emoji():
    assert _clean_caption("Bulge 👤 between 🔥 her legs") == "Bulge between her legs"
    assert _clean_caption("no emoji here") == "no emoji here"


def test_wrap_caption_coupe_en_lignes():
    lines = _wrap_caption("is cake even if it has a candle they still eat it right", max_chars=20)
    assert len(lines) >= 2
    assert all(len(x) <= 20 for x in lines)
    # aucun mot perdu
    assert " ".join(lines).split() == "is cake even if it has a candle they still eat it right".split()


def test_mix_musique():
    cmd = build_assemble_command(
        "in.mp4", "out.mp4", AssembleParams("720p", "standard", music_path="music.mp3")
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "amix=inputs=2:duration=first" in fc
    assert "volume=-18.0dB" in fc
    assert "-stream_loop" in cmd  # musique bouclée sur la durée de la vidéo
    assert "[vout]" in fc and "[aout]" in fc
