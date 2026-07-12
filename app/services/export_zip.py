"""Construit un ZIP des seuls médias générés d'un job (photos ou vidéos).
Chaque média est téléchargé via l'anti-SSRF safe_download ; un média qui échoue
est simplement ignoré (il n'interrompt pas le reste et n'ajoute aucun fichier)."""

import io
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from app.net import safe_download


def _ext_from_url(url: str, default: str) -> str:
    ext = Path(urlparse(url).path).suffix.lstrip(".").lower()
    return ext or default


def build_media_zip(entries: list[dict], default_ext: str) -> bytes:
    """entries : liste de {item_id, url, prompt}. Renvoie les octets d'un zip
    contenant UNIQUEMENT les médias (numérotés), sans manifest ni JSON."""
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(
        buf, "w", zipfile.ZIP_DEFLATED
    ) as zf:
        for i, e in enumerate(entries, start=1):
            ext = _ext_from_url(e["url"], default_ext)
            name = f"{i:03d}_{str(e['item_id'])[:8]}.{ext}"
            try:
                dest = Path(tmp) / name
                safe_download(e["url"], dest)
                zf.write(dest, name)
            except Exception:  # un média HS ne fait pas échouer tout le zip
                continue
    return buf.getvalue()
