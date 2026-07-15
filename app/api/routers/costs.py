"""Tableau de bord des coûts : agrège la dépense (vidéo + photo) par fenêtre
temporelle (7 j / 30 j / 90 j / ce mois / total) et par mois, scopé au tenant.

Coût par job = coût RÉEL (actual_cost_usd) s'il est connu, sinon l'estimé
(estimated_cost_usd) pour les jobs encore en cours — jamais de valeur devinée.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.db.base import get_db
from app.db.models import GenerationJob, PictureJob, User

router = APIRouter(prefix="/api/costs", tags=["costs"])


def _job_cost(actual: float | None, estimated: float | None) -> float:
    """Réel si connu, sinon estimé (job en cours), sinon 0."""
    return float(actual if actual is not None else (estimated or 0.0))


def _aware(ts: datetime | None) -> datetime | None:
    """created_at → UTC aware (SQLite peut renvoyer du naïf)."""
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


@router.get("/summary")
def cost_summary(db: Session = Depends(get_db), user: User = Depends(current_user)):
    now = datetime.now(timezone.utc)

    def rows(model):
        return [
            (_aware(created), _job_cost(actual, est))
            for created, actual, est in db.execute(
                select(model.created_at, model.actual_cost_usd, model.estimated_cost_usd)
                .where(model.tenant_id == user.tenant_id)
            ).all()
        ]

    video = rows(GenerationJob)
    photo = rows(PictureJob)

    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    windows = [
        ("7d", "7 derniers jours", now - timedelta(days=7)),
        ("30d", "30 derniers jours", now - timedelta(days=30)),
        ("90d", "90 derniers jours", now - timedelta(days=90)),
        ("month", "Ce mois-ci", month_start),
        ("all", "Total (depuis le début)", None),
    ]

    def bucket(entries, since):
        return [c for ts, c in entries if since is None or (ts is not None and ts >= since)]

    out_windows = []
    for key, label, since in windows:
        v = round(sum(bucket(video, since)), 4)
        p = round(sum(bucket(photo, since)), 4)
        out_windows.append({
            "key": key, "label": label,
            "video_usd": v, "photo_usd": p, "total_usd": round(v + p, 4),
            "video_jobs": len(bucket(video, since)), "photo_jobs": len(bucket(photo, since)),
        })

    # Décompte par mois (12 derniers mois présents), pour la tendance mensuelle.
    by_month: dict[str, dict] = {}
    for entries, field in ((video, "video_usd"), (photo, "photo_usd")):
        for ts, c in entries:
            if ts is None:
                continue
            key = f"{ts.year:04d}-{ts.month:02d}"
            m = by_month.setdefault(key, {"month": key, "video_usd": 0.0, "photo_usd": 0.0})
            m[field] += c
    months = []
    for key in sorted(by_month, reverse=True)[:12]:
        m = by_month[key]
        months.append({
            "month": key,
            "video_usd": round(m["video_usd"], 4),
            "photo_usd": round(m["photo_usd"], 4),
            "total_usd": round(m["video_usd"] + m["photo_usd"], 4),
        })

    return {"currency": "USD", "windows": out_windows, "by_month": months}
