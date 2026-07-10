"""QC gate face-match (brief §9).

Frame échantillonnée de la vidéo brute (FFmpeg) → embedding facial ArcFace
(insightface) → similarité cosinus vs la photo de référence de la model →
sous le seuil = fail (rejet auto). Empêche la dérive de visage de polluer
les livrables à l'échelle.

insightface/onnxruntime sont lourds → extra optionnel `[qc]`, import lazy,
et QC_ENABLED=false par défaut. La partie numérique (cosine) reste pure.
"""

import subprocess
from functools import lru_cache

import numpy as np

from app.config import get_settings


class QcError(RuntimeError):
    pass


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    if norm == 0:
        return 0.0
    return float(np.dot(a, b) / norm)


def build_extract_frame_command(video_path: str, out_jpg: str, at_s: float) -> list[str]:
    return [
        get_settings().ffmpeg_bin,
        "-y",
        "-ss", f"{at_s:.3f}",
        "-i", video_path,
        "-frames:v", "1",
        "-q:v", "2",
        out_jpg,
    ]


def extract_frame(video_path: str, out_jpg: str, at_s: float | None = None) -> str:
    at_s = get_settings().qc_frame_time_s if at_s is None else at_s
    proc = subprocess.run(
        build_extract_frame_command(video_path, out_jpg, at_s), capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise QcError(f"Extraction frame échouée : {proc.stderr[-1000:]}")
    return out_jpg


@lru_cache
def _face_analyzer():
    try:
        from insightface.app import FaceAnalysis
    except ImportError as exc:
        raise QcError(
            "QC_ENABLED=true mais insightface n'est pas installé — "
            "pip install -e '.[qc]'"
        ) from exc
    analyzer = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
    analyzer.prepare(ctx_id=-1, det_size=(640, 640))
    return analyzer


def face_embedding(image_path: str) -> np.ndarray:
    import cv2

    image = cv2.imread(image_path)
    if image is None:
        raise QcError(f"Image illisible : {image_path}")
    faces = _face_analyzer().get(image)
    if not faces:
        raise QcError(f"Aucun visage détecté dans {image_path}")
    # Le plus grand visage de l'image (la model au premier plan)
    face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
    return face.normed_embedding


def face_match_score(frame_jpg: str, reference_jpg: str) -> float:
    """Similarité cosinus entre le visage de la frame et la référence."""
    return cosine_similarity(face_embedding(frame_jpg), face_embedding(reference_jpg))


def check_video(video_path: str, reference_jpg: str, workdir: str) -> tuple[float, bool]:
    """Renvoie (score, passe). Une frame sans visage détectable = fail (score 0)."""
    frame = f"{workdir}/qc_frame.jpg"
    extract_frame(video_path, frame)
    try:
        score = face_match_score(frame, reference_jpg)
    except QcError as exc:
        if "Aucun visage" in str(exc):
            return 0.0, False
        raise
    return score, score >= get_settings().qc_threshold
