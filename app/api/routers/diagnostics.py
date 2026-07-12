"""Diagnostic système : état worker/broker + repérage des URLs d'images cassées
(placeholder R2 laissé après un mauvais R2_PUBLIC_BASE_URL). Protégé par auth.

But : donner à l'utilisateur une réponse claire quand une génération reste
« pending » (worker/broker) ou échoue (images de référence non téléchargeables)."""

import json

from fastapi import APIRouter, Depends
from fastapi.responses import Response
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


# kind machine → (libellé affiché, modèle SQLAlchemy supprimable). Le visage
# model n'est PAS supprimable ici (ce serait supprimer la model entière) : il est
# seulement signalé pour ré-upload manuel via l'édition de la fiche model.
_DELETABLE = {
    "characteristic": ("Caractéristique", ModelCharacteristic),
    "outfit": ("Outfit", Outfit),
    "background": ("Background", Background),
}


def _collect_reference_assets(db: Session, tid: str) -> list[dict]:
    """Énumère toutes les images de référence vidéo (visage, caractéristiques,
    outfits, backgrounds) avec leur identité DB (kind + id) pour pouvoir les
    tester ET les supprimer précisément."""
    assets: list[dict] = []
    for m in db.scalars(select(Model).where(Model.tenant_id == tid)).all():
        if m.face_reference_url:
            assets.append({"kind": "model_face", "id": str(m.id), "type": "Visage model",
                           "label": m.name, "url": m.face_reference_url})
    for c in db.scalars(
        select(ModelCharacteristic)
        .join(Model, ModelCharacteristic.model_id == Model.id)
        .where(Model.tenant_id == tid)
    ).all():
        assets.append({"kind": "characteristic", "id": str(c.id), "type": "Caractéristique",
                       "label": c.label, "url": c.reference_image_url})
    for o in db.scalars(select(Outfit).where(Outfit.tenant_id == tid)).all():
        assets.append({"kind": "outfit", "id": str(o.id), "type": "Outfit",
                       "label": ", ".join(o.tags) or str(o.id)[:8], "url": o.image_url})
    for b in db.scalars(select(Background).where(Background.tenant_id == tid)).all():
        assets.append({"kind": "background", "id": str(b.id), "type": "Background",
                       "label": ", ".join(b.tags) or str(b.id)[:8], "url": b.image_url})
    return assets


def _probe_assets(assets: list[dict]) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor

    if not assets:
        return []
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(lambda a: {**a, **_probe_url(a["url"])}, assets))


def _is_problem(r: dict) -> bool:
    """Hors bornes Seedance (300–6000 px) OU non téléchargeable."""
    return r.get("status") != 200 or r.get("seedance_ok") is False


@router.get("/reference-dimensions")
def reference_dimensions(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Scanne TOUTES les images de référence et renvoie précisément lesquelles
    sont hors des bornes Seedance (300–6000 px) ou non téléchargeables."""
    results = _probe_assets(_collect_reference_assets(db, user.tenant_id))
    problems = [r for r in results if _is_problem(r)]
    return {
        "total_checked": len(results),
        "problems_count": len(problems),
        "min_px": SEEDANCE_MIN_PX,
        "max_px": SEEDANCE_MAX_PX,
        "problems": problems,  # {kind, id, type, label, url, status, width, height, seedance_ok/error}
    }


def _slug(text: str) -> str:
    keep = "".join(c if c.isalnum() else "-" for c in (text or "")).strip("-")
    return (keep or "image")[:40]


@router.post("/reference-dimensions/purge")
def purge_bad_reference_images(
    db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Télécharge dans un ZIP toutes les images de référence fautives (hors
    300–6000 px ou non téléchargeables) PUIS les retire de leur pool (outfits,
    caractéristiques, backgrounds). Le visage d'une model n'est pas supprimé
    (ce serait supprimer la model) : il est mis dans le ZIP et signalé pour
    ré-upload manuel. Renvoie le ZIP ; le résumé de purge est dans l'en-tête
    X-Purge-Summary."""
    import tempfile
    import zipfile
    from pathlib import Path

    from app.net import safe_download

    problems = [r for r in _probe_assets(_collect_reference_assets(db, user.tenant_id)) if _is_problem(r)]

    # 1) ZIP des images fautives téléchargeables (pour recadrage/ré-upload)
    buf = __import__("io").BytesIO()
    zipped = 0
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, p in enumerate(problems, start=1):
            ext = _ext_from_url(p["url"])
            name = f"{i:03d}_{p['kind']}_{_slug(p.get('label'))}.{ext}"
            try:
                dest = Path(tmp) / name
                safe_download(p["url"], dest)
                zf.write(dest, name)
                zipped += 1
            except Exception:
                continue  # image injoignable : impossible de la sauver, on la supprimera quand même

    # 2) suppression des lignes de pool (hors visage model)
    deleted = {"characteristic": 0, "outfit": 0, "background": 0}
    faces_to_fix: list[str] = []
    for p in problems:
        if p["kind"] == "model_face":
            faces_to_fix.append(p.get("label") or p["id"])
            continue
        display, model = _DELETABLE[p["kind"]]
        import uuid as _uuid

        row = db.get(model, _uuid.UUID(p["id"]))
        if row is None:
            continue
        # Les assets viennent déjà d'une requête scopée au tenant. Défense en
        # profondeur : si la ligne porte un tenant_id (outfit/background), il doit
        # correspondre ; la caractéristique n'en a pas (scopée via sa model).
        row_tid = getattr(row, "tenant_id", None)
        if row_tid is not None and row_tid != user.tenant_id:
            continue
        db.delete(row)
        deleted[p["kind"]] += 1
    db.commit()

    summary = {
        "problems_found": len(problems),
        "zipped": zipped,
        "deleted": deleted,
        "deleted_total": sum(deleted.values()),
        "faces_to_fix": faces_to_fix,  # visages hors bornes → ré-upload manuel
    }
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="images_hors_dimension.zip"',
            "X-Purge-Summary": json.dumps(summary, ensure_ascii=False),
            "Access-Control-Expose-Headers": "X-Purge-Summary",
        },
    )


def _ext_from_url(url: str) -> str:
    from pathlib import Path
    from urllib.parse import urlparse

    return (Path(urlparse(url).path).suffix.lstrip(".").lower()) or "jpg"


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
