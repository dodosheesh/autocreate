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
    # Nano Banana (Gemini image) — ~0,09 $/image (ancien modèle photo).
    ("nano_banana", None, None, "per_image", 0.09),
    # Seedream 5.0 Pro (ByteDance) sur kie.ai — un tarif PAR résolution (l'estimateur
    # s'adapte selon le choix 1K/2K du job) : 1K = 7 crédits (0,035 $), 2K = 14
    # crédits (0,07 $). Images d'entrée : 0,5 crédit (0,0025 $), 1re gratuite.
    ("seedream", "1K", None, "per_image", 0.035),
    ("seedream", "2K", None, "per_image", 0.07),
]


# Migrations additives idempotentes (pas d'Alembic pour l'instant) : colonnes
# ajoutées APRÈS le schéma initial. create_all() crée les tables manquantes mais
# jamais les colonnes manquantes d'une table existante → on les ajoute ici.
# (table, colonne, définition SQL Postgres)
_ADDITIVE_COLUMNS = [
    ("prompt_templates", "status", "VARCHAR(16) DEFAULT 'ready'"),
    ("prompt_templates", "source_video_url", "TEXT"),
    ("prompt_templates", "error", "TEXT"),
    ("outfits", "status", "VARCHAR(16) DEFAULT 'ready'"),
    ("outfits", "error", "TEXT"),
    ("backgrounds", "status", "VARCHAR(16) DEFAULT 'ready'"),
    ("backgrounds", "error", "TEXT"),
    ("job_items", "review_status", "VARCHAR(16) DEFAULT 'pending'"),
    ("generation_jobs", "music_url", "TEXT"),
    ("generation_jobs", "compose_shortfall", "JSON DEFAULT '{}'"),
    ("generation_jobs", "error", "TEXT"),
    ("users", "token_version", "INTEGER DEFAULT 0"),
    ("model_characteristics", "recurring", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("model_characteristics", "seedream", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("job_items", "generation_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("picture_items", "generation_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("picture_jobs", "image_resolution", "VARCHAR(8) NOT NULL DEFAULT '2K'"),
    ("picture_jobs", "styles", "JSON DEFAULT '[]'"),
]


def ensure_columns() -> None:
    """Ajoute les colonnes récentes si absentes (Postgres ADD COLUMN IF NOT
    EXISTS). No-op sur SQLite (les tests partent d'un create_all frais)."""
    if engine.dialect.name != "postgresql":
        return
    from sqlalchemy import text

    with engine.begin() as conn:
        for table, column, ddl in _ADDITIVE_COLUMNS:
            conn.execute(
                text(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}')
            )


def init() -> None:
    settings = get_settings()
    Base.metadata.create_all(engine)
    ensure_columns()
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

    # Garde-fou config R2 : si l'URL publique est vide ou reste un placeholder,
    # AUCUNE image uploadée ne sera téléchargeable (vision + génération échouent).
    pub = (settings.r2_public_base_url or "").lower()
    placeholders = ("remplacer", "xxxx", "replace", "example", "ton-", "your-")
    if not pub or any(tok in pub for tok in placeholders):
        print(
            "⚠️  R2_PUBLIC_BASE_URL semble non configuré "
            f"({settings.r2_public_base_url!r}) : les images uploadées ne seront "
            "pas téléchargeables → descriptions et générations échoueront. "
            "Mets l'URL publique r2.dev réelle de ton bucket."
        )
    print("Tables créées et pricing seedé.")


if __name__ == "__main__":
    init()
