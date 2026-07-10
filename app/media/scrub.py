"""Nettoyage complet des métadonnées d'une image avant téléchargement.

Décode les pixels et ré-encode dans un fichier neuf : tout ce qui est
ancillaire — EXIF, XMP, IPTC, profil ICC, chunks de texte PNG, manifeste
de provenance C2PA — est laissé de côté, seuls les pixels sont réécrits.

⚠️ SynthID (Google) est un watermark encodé dans les PIXELS eux-mêmes, pas
dans les métadonnées : ce scrub ne le retire pas et n'est pas conçu pour.
Il retire l'étiquette « généré par IA » lisible (métadonnées / C2PA), pas
le filigrane de provenance pixel.
"""

from PIL import Image


def strip_metadata(in_path: str, out_path: str, output_format: str | None = None) -> str:
    """Réécrit l'image sans aucune métadonnée. `output_format` force le
    format de sortie (déduit de l'extension sinon)."""
    with Image.open(in_path) as src:
        src.load()
        mode = src.mode
        size = src.size
        raw = src.tobytes()

    # Image neuve reconstruite depuis les seuls octets de pixels → zéro
    # info héritée (EXIF/XMP/IPTC/ICC/C2PA/chunks texte tous laissés de côté)
    clean = Image.frombytes(mode, size, raw)

    fmt = (output_format or "").upper() or None
    save_kwargs = {}
    if fmt in ("JPG", "JPEG") or (fmt is None and out_path.lower().endswith((".jpg", ".jpeg"))):
        fmt = "JPEG"
        if clean.mode not in ("RGB", "L"):
            clean = clean.convert("RGB")
        save_kwargs["quality"] = 95
    # Pas d'exif=, pas d'icc_profile, pas de pnginfo → rien n'est écrit
    clean.save(out_path, format=fmt, **save_kwargs)
    return out_path
