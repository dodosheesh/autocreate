"""Pont DB → estimateur pur : charge la table pricing en liste de Rate."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Pricing
from app.services.estimator import Rate


def load_rates(db: Session) -> list[Rate]:
    rows = db.scalars(select(Pricing)).all()
    return [
        Rate(
            model=r.model,
            resolution=r.resolution,
            with_ref=r.with_ref,
            unit=r.unit,
            rate_usd=r.rate_usd,
        )
        for r in rows
    ]
