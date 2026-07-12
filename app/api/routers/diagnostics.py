"""Diagnostic système : état worker/broker + repérage des URLs d'images cassées
(placeholder R2 laissé après un mauvais R2_PUBLIC_BASE_URL). Protégé par auth.

But : donner à l'utilisateur une réponse claire quand une génération reste
« pending » (worker/broker) ou échoue (images de référence non téléchargeables)."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db.base import get_db
from app.db.models import (
    Background,
    Model,
    ModelCharacteristic,
    Outfit,
    PicturePrompt,
    User,
)
from app.workers.celery_app import celery_app

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])

# Jetons trahissant une URL publique R2 laissée en placeholder (donc non
# téléchargeable par kie.ai / la vision).
_PLACEHOLDER_TOKENS = ("remplacer", "xxxx", "replace", "example", "ton-", "your-", "pub-xxxx")


def _is_placeholder(url: str | None) -> bool:
    u = (url or "").lower()
    return (not u) or any(tok in u for tok in _PLACEHOLDER_TOKENS)


# Seedance (vidéo) exige des images de référence entre 300 et 6000 px de côté.
SEEDANCE_MIN_PX, SEEDANCE_MAX_PX = 300, 6000


def _probe_url(url: str) -> dict:
    """Télécharge l'image (anti-SSRF) et renvoie son accessibilité + dimensions.
    Sert à repérer une image non publique OU hors des bornes 300–6000 px que
    Seedance refuse (« Width must be between 300px and 6000px »)."""
    import tempfile
    from pathlib import Path

    from PIL import Image

    from app.net import safe_download

    try:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "probe"
            safe_download(url, dest)
            with Image.open(dest) as im:
                w, h = im.size
        in_range = SEEDANCE_MIN_PX <= min(w, h) and max(w, h) <= SEEDANCE_MAX_PX
        return {"status": 200, "width": w, "height": h, "seedance_ok": in_range}
    except Exception as exc:
        return {"status": "unreachable", "error": str(exc)[:150]}


def _broker_ok() -> bool:
    try:
        conn = celery_app.connection()
        conn.ensure_connection(max_retries=1, timeout=2)
        conn.release()
        return True
    except Exception:
        return False


def _workers() -> list[str]:
    try:
        replies = celery_app.control.ping(timeout=2) or []
        return [name for reply in replies for name in reply.keys()]
    except Exception:
        return []


@router.get("/reference-dimensions")
def reference_dimensions(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Scanne TOUTES les images de référence (visage, caractéristiques, outfits,
    backgrounds) et renvoie précisément lesquelles sont hors des bornes Seedance
    (300–6000 px) ou non téléchargeables — pour savoir exactement quoi ré-uploader.

    (L'échantillon du diagnostic principal ne teste qu'une image par type ; ici on
    les teste toutes, avec leur nom/label.)"""
    from concurrent.futures import ThreadPoolExecutor

    tid = user.tenant_id
    assets: list[dict] = []
    for m in db.scalars(select(Model).where(Model.tenant_id == tid)).all():
        if m.face_reference_url:
            assets.append({"type": "Visage model", "label": m.name, "url": m.face_reference_url})
    for c in db.scalars(
        select(ModelCharacteristic)
        .join(Model, ModelCharacteristic.model_id == Model.id)
        .where(Model.tenant_id == tid)
    ).all():
        assets.append({"type": "Caractéristique", "label": c.label, "url": c.reference_image_url})
    for o in db.scalars(select(Outfit).where(Outfit.tenant_id == tid)).all():
        assets.append({"type": "Outfit", "label": ", ".join(o.tags) or str(o.id)[:8], "url": o.image_url})
    for b in db.scalars(select(Background).where(Background.tenant_id == tid)).all():
        assets.append({"type": "Background", "label": ", ".join(b.tags) or str(b.id)[:8], "url": b.image_url})

    def _check(a: dict) -> dict:
        return {**a, **_probe_url(a["url"])}

    if assets:
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(_check, assets))
    else:
        results = []

    problems = [
        r for r in results
        if r.get("status") != 200 or r.get("seedance_ok") is False
    ]
    return {
        "total_checked": len(results),
        "problems_count": len(problems),
        "min_px": SEEDANCE_MIN_PX,
        "max_px": SEEDANCE_MAX_PX,
        "problems": problems,  # {type, label, url, status, width, height, seedance_ok/error}
    }


@router.get("")
def diagnostics(db: Session = Depends(get_db), user: User = Depends(current_user)):
    tid = user.tenant_id

    # --- images de référence cassées (URL placeholder en base) ---
    broken: dict[str, int] = {}

    faces = db.scalars(select(Model.face_reference_url).where(Model.tenant_id == tid)).all()
    broken["model_faces"] = sum(_is_placeholder(u) for u in faces)

    char_urls = db.scalars(
        select(ModelCharacteristic.reference_image_url)
        .join(Model, ModelCharacteristic.model_id == Model.id)
        .where(Model.tenant_id == tid)
    ).all()
    broken["characteristics"] = sum(_is_placeholder(u) for u in char_urls)

    for label, model, col in (
        ("outfits", Outfit, Outfit.image_url),
        ("backgrounds", Background, Background.image_url),
        ("picture_prompts", PicturePrompt, PicturePrompt.source_image_url),
    ):
        urls = db.scalars(select(col).where(model.tenant_id == tid)).all()
        broken[label] = sum(_is_placeholder(u) for u in urls)

    # --- reachability RÉELLE d'un échantillon d'images (comme kie.ai le ferait) ---
    # « internal error » de kie.ai vient souvent d'images non téléchargeables
    # (bucket R2 pas réellement public). On teste 1 URL par type.
    samples: dict[str, str] = {}
    if faces:
        samples["model_face"] = faces[0]
    if char_urls:
        samples["characteristic"] = char_urls[0]
    for label, model, col in (("outfit", Outfit, Outfit.image_url), ("background", Background, Background.image_url)):
        u = db.scalar(select(col).where(model.tenant_id == tid).limit(1))
        if u:
            samples[label] = u
    reachable = {label: _probe_url(url) for label, url in samples.items()}
    all_public = all(p.get("status") == 200 for p in reachable.values())
    # Dimensions hors bornes Seedance (300–6000 px) → cause du « Width must be… »
    dim_issues = {
        label: f"{p['width']}×{p['height']}"
        for label, p in reachable.items()
        if p.get("status") == 200 and not p.get("seedance_ok")
    }

    workers = _workers()
    return {
        "broker_reachable": _broker_ok(),
        "workers_online": len(workers),
        "worker_names": workers,
        "broken_reference_urls": broken,
        "broken_total": sum(broken.values()),
        "image_reachability": reachable,  # {status, width, height, seedance_ok} par type
        "images_public_ok": all_public,
        "seedance_dimension_issues": dim_issues,  # images hors 300–6000 px (vidéo)
    }
