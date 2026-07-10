import numpy as np

from app.services.qc import build_extract_frame_command, cosine_similarity


def test_cosine_identique():
    v = np.array([0.2, 0.5, -0.3])
    assert cosine_similarity(v, v) == 1.0


def test_cosine_orthogonal_et_oppose():
    a = np.array([1.0, 0.0])
    b = np.array([0.0, 1.0])
    assert cosine_similarity(a, b) == 0.0
    assert cosine_similarity(a, -a) == -1.0


def test_cosine_vecteur_nul():
    assert cosine_similarity(np.zeros(3), np.ones(3)) == 0.0


def test_extract_frame_command():
    cmd = build_extract_frame_command("video.mp4", "frame.jpg", 1.0)
    joined = " ".join(cmd)
    assert "-ss 1.000" in joined
    assert "-frames:v 1" in joined
