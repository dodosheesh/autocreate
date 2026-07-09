"""Initialisation Phase 1 : crée les tables + seed la table pricing.

Usage : python -m app.db.init_db

(Alembic prendra le relais en Phase 2 pour les migrations ; pour l'instant
create_all suffit sur une base vierge.)

Les valeurs seedées sont les tarifs indicatifs du brief — à resynchroniser
sur kie.ai/pricing avant tout batch réel (la table reste la source de vérité,
le code ne lit jamais un tarif en dur).
"""

from sqlalchemy import select

from app.db.base import Base, SessionLocal, engine
from app.db.models import Pricing

SEED_PRICING = [
    # (model, resolution, with_ref, unit, rate_usd)
    ("seedance_2.0", "480p", True, "per_sec", 0.0575),
    ("seedance_2.0", "720p", True, "per_sec", 0.125),
    ("seedance_2.0", "1080p", True, "per_sec", 0.31),
    ("elevenlabs_s2s", None, None, "per_min", 0.10),
]


def init() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        for model, resolution, with_ref, unit, rate in SEED_PRICING:
            exists = db.scalar(
                select(Pricing).where(
                    Pricing.model == model,
                    Pricing.resolution == resolution,
                    Pricing.with_ref == with_ref,
                    Pricing.unit == unit,
                )
            )
            if not exists:
                db.add(
                    Pricing(
                        model=model,
                        resolution=resolution,
                        with_ref=with_ref,
                        unit=unit,
                        rate_usd=rate,
                    )
                )
        db.commit()
    print("Tables créées et pricing seedé.")


if __name__ == "__main__":
    init()
