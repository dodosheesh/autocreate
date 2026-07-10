"""Initialisation Phase 1 : crée les tables + seed la table pricing.

Usage : python -m app.db.init_db

(Alembic prendra le relais en Phase 2 pour les migrations ; pour l'instant
create_all suffit sur une base vierge.)

Les valeurs seedées sont les tarifs indicatifs du brief — à resynchroniser
sur kie.ai/pricing avant tout batch réel (la table reste la source de vérité,
le code ne lit jamais un tarif en dur).
"""

import secrets

from sqlalchemy import select

from app.config import get_settings
from app.db.base import Base, SessionLocal, engine
from app.db.models import Pricing, User
from app.services.security import hash_password

SEED_PRICING = [
    # (model, resolution, with_ref, unit, rate_usd)
    # Seedance 2.0 FAST — facturé À LA SECONDE. On envoie des images de référence
    # (pas de vidéo) → tier « no video input », le plus cher à l'unité :
    #   480p = 0.0775 $/s, 720p = 0.165 $/s (source : page kie.ai seedance-2-fast).
    # (Le 1080p n'existe pas en Fast ; la ligne sert si tu repasses en Standard.)
    # Resynchronise les valeurs EXACTES de ton compte via l'éditeur de tarifs.
    ("seedance_2.0", "480p", True, "per_sec", 0.0775),
    ("seedance_2.0", "720p", True, "per_sec", 0.165),
    ("seedance_2.0", "1080p", True, "per_sec", 0.31),
    # ElevenLabs Voice Changer — 1000 crédits/min ; ~0,12 $/min pay-as-you-go.
    ("elevenlabs_s2s", None, None, "per_min", 0.12),
    # Nano Banana Pro (Gemini 3 Pro Image) — ~0,09 $/image en 1K/2K (0,12 en 4K).
    ("nano_banana", None, None, "per_image", 0.09),
]


def init() -> None:
    settings = get_settings()
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

        # Compte propriétaire (une seule fois). Jamais de mot de passe par
        # défaut exploitable : si BOOTSTRAP_ADMIN_PASSWORD n'est pas fourni,
        # on en génère un aléatoire et on l'imprime une seule fois.
        admin_email = settings.bootstrap_admin_email.lower()
        if admin_email and not db.scalar(select(User).where(User.email == admin_email)):
            password = settings.bootstrap_admin_password or secrets.token_urlsafe(18)
            generated = not settings.bootstrap_admin_password
            db.add(
                User(
                    email=admin_email,
                    password_hash=hash_password(password),
                    role="owner",
                )
            )
            print(f"Compte propriétaire créé : {admin_email}")
            if generated:
                print(f"  Mot de passe initial (à noter, non ré-affiché) : {password}")
            else:
                print("  Mot de passe : via BOOTSTRAP_ADMIN_PASSWORD — à changer au 1er login")
        db.commit()
    print("Tables créées et pricing seedé.")


if __name__ == "__main__":
    init()
