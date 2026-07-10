"""Upload d'assets depuis l'UI → R2 → URL publique.

Sert à alimenter les banques sans coller d'URL R2 à la main : l'UI envoie le
fichier ici, on le pousse sur R2 sous une clé scopée au tenant, et on renvoie
l'URL publique à réutiliser (face_reference_url, image_url d'outfit, etc.).
"""

import os
import tempfile
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.api.deps import current_user
from app.db.models import User
from app.integrations import r2

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

# content_type → extension. Images pour models/caractéristiques/outfits/
# backgrounds ; audio pour une éventuelle piste musique.
ALLOWED_TYPES = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    # vidéos de référence (reverse-engineering vidéo)
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/webm": "webm",
}
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 Mo (vidéos de référence)


@router.post("")
async def upload(file: UploadFile = File(...), user: User = Depends(current_user)):
    ext = ALLOWED_TYPES.get((file.content_type or "").lower())
    if ext is None:
        raise HTTPException(
            415, f"Type non supporté : {file.content_type!r} (images ou audio uniquement)"
        )
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Fichier > {MAX_UPLOAD_BYTES // (1024 * 1024)} Mo")
    if not content:
        raise HTTPException(422, "Fichier vide")

    key = f"uploads/{user.tenant_id}/{uuid.uuid4().hex}.{ext}"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        url = r2.upload_file(tmp_path, key, content_type=file.content_type)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
    return {"url": url, "key": key}
