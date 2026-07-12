"""Construit un ZIP des médias générés d'un job (photos ou vidéos) + un
manifest.json. Chaque média est téléchargé via l'anti-SSRF safe_download ; un
média qui échoue est noté dans le manifest sans interrompre le reste."""

import io
import json
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
    contenant les médias (numérotés) + manifest.json."""
    manifest: list[dict] = []
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(
        buf, "w", zipfile.ZIP_DEFLATED
    ) as zf:
        for i, e in enumerate(entries, start=1):
            ext = _ext_from_url(e["url"], default_ext)
            name = f"{i:03d}_{str(e['item_id'])[:8]}.{ext}"
            row = {
                "file": name,
                "item_id": str(e["item_id"]),
                "url": e["url"],
                "prompt": e.get("prompt"),
            }
            try:
                dest = Path(tmp) / name
                safe_download(e["url"], dest)
                zf.write(dest, name)
            except Exception as exc:  # un média HS ne fait pas échouer tout le zip
                row["file"] = None
                row["error"] = str(exc)[:300]
            manifest.append(row)
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    return buf.getvalue()
