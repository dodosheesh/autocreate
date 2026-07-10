from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/autocreate"
    redis_url: str = "redis://localhost:6379/0"

    kie_api_key: str = ""
    kie_base_url: str = "https://api.kie.ai"
    kie_seedance_model: str = "bytedance/seedance-2"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
