from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET_SENTINEL = "dev-insecure-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/autocreate"
    redis_url: str = "redis://localhost:6379/0"

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalize_db_url(cls, value: str) -> str:
        """Railway/Heroku fournissent `postgres://` ou `postgresql://` ;
        SQLAlchemy + psycopg 3 exige le driver explicite `postgresql+psycopg://`.
        On normalise pour que l'URL Railway marche sans retouche manuelle."""
        if not isinstance(value, str):
            return value
        for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
            if value.startswith(prefix):
                return value
        if value.startswith("postgresql://"):
            return "postgresql+psycopg://" + value[len("postgresql://"):]
        if value.startswith("postgres://"):
            return "postgresql+psycopg://" + value[len("postgres://"):]
        return value

    kie_api_key: str = ""
    kie_base_url: str = "https://api.kie.ai"
    # Seedance 2.0 Fast : 480p/720p, ~4 min/génération, moins cher. 720p suffit
    # pour des Reels. (Standard = bytedance/seedance-2, ajoute le 1080p.)
    kie_seedance_model: str = "bytedance/seedance-2-fast"
    # Nano Banana EDIT (Gemini 2.5 Flash Image, image-to-image) : c'est le modèle
    # qui accepte des images de référence (visage + caractéristiques) → consistance
    # du personnage. Input : {prompt, image_urls, image_size, output_format}.
    # (« google/nano-banana-pro » est refusé : model name not supported. Le Pro
    # 4K/Gemini 3 utilise un autre schéma d'input `image_input` — non branché ici.)
    kie_nano_banana_model: str = "google/nano-banana-edit"
    # Modèle vision pour le reverse-engineering image → prompt (endpoint
    # OpenAI-compatible de kie.ai). Le modèle et l'URL sont configurables :
    # aucune valeur devinée, l'utilisateur pointe le modèle vision de son choix.
    kie_vision_model: str = "google/gemini-3-pro"
    kie_vision_base_url: str = "https://api.kie.ai/gemini-3-pro/v1"
    # Vision par Claude (Anthropic) : si ANTHROPIC_API_KEY est fourni, l'analyse
    # d'images (auto-description, reverse-engineering) passe par Claude au lieu de
    # kie.ai/Gemini. Sinon on garde le chemin kie.ai. Rien n'est deviné en dur.
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_vision_model: str = "claude-haiku-4-5-20251001"
    # Nombre max d'images de référence acceptées par nano-banana-edit
    nano_banana_max_refs: int = 10
    # Re-essais auto sur échec TRANSITOIRE de génération kie.ai (« internal error,
    # try again later »). N'affecte pas les refus définitifs (contenu sensible…).
    generation_max_retries: int = 2

    public_base_url: str = "http://localhost:8000"

    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = "autocreate"
    r2_public_base_url: str = ""

    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    # Contrainte Seedance 2.0 : nombre max d'images de référence par génération
    seedance_max_refs: int = 12

    # Taux de réussite QC par défaut pour l'estimateur (recalibré via calibration_log)
    default_qc_success_rate: float = 0.80

    # --- ElevenLabs (voice-swap speech-to-speech) ---
    elevenlabs_api_key: str = ""
    elevenlabs_base_url: str = "https://api.elevenlabs.io"
    elevenlabs_sts_model: str = "eleven_multilingual_sts_v2"
    # stability haut = delivery plus stable inter-reels ;
    # similarity_boost haut = plus collé au timbre cible
    elevenlabs_stability: float = 0.5
    elevenlabs_similarity_boost: float = 0.75
    voice_swap_enabled: bool = True

    # --- QC face-match ---
    # Nécessite l'extra [qc] (insightface + onnxruntime) ; off par défaut
    qc_enabled: bool = False
    qc_threshold: float = 0.35  # similarité cosinus ArcFace min
    qc_frame_time_s: float = 1.0  # instant de la frame échantillonnée

    # --- Segmentation voix (VAD énergie) ---
    vad_min_silence_s: float = 0.25  # gap min entre deux segments de parole
    vad_min_speech_s: float = 0.15  # durée min d'un segment retenu

    # --- Assemblage ---
    music_volume_db: float = -18.0  # volume de la piste musique sous la voix

    # --- Auth ---
    # Clé de signature des cookies de session — OBLIGATOIRE de la surcharger
    # en prod (générer : python -c "import secrets; print(secrets.token_hex(32))")
    secret_key: str = DEV_SECRET_SENTINEL
    session_ttl_s: int = 60 * 60 * 24 * 14  # 14 jours
    session_cookie: str = "autocreate_session"
    # Force le flag Secure du cookie ; None = déduit du https de public_base_url
    # (mettre True derrière un proxy TLS où public_base_url resterait en http)
    session_cookie_secure: bool | None = None
    # Compte propriétaire seedé au premier init_db. Vide = mot de passe aléatoire
    # généré et imprimé une fois (jamais de mot de passe par défaut exploitable).
    bootstrap_admin_email: str = "sydeincovind@gmail.com"
    bootstrap_admin_password: str = ""
    # Secret partagé kie.ai pour authentifier les webhooks (?secret=...)
    kie_webhook_secret: str = ""

    @property
    def is_production(self) -> bool:
        return self.public_base_url.startswith("https")

    @property
    def cookie_secure(self) -> bool:
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return self.is_production

    def assert_secure_config(self) -> None:
        """Bloque un démarrage en prod avec des secrets par défaut (fail-fast)."""
        if not self.is_production:
            return
        problems = []
        if self.secret_key == DEV_SECRET_SENTINEL or len(self.secret_key) < 32:
            problems.append("SECRET_KEY (>= 32 octets aléatoires requis en prod)")
        if not self.kie_webhook_secret:
            problems.append("KIE_WEBHOOK_SECRET (requis pour authentifier les webhooks)")
        if problems:
            raise RuntimeError(
                "Configuration non sécurisée pour la production : "
                + ", ".join(problems)
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
