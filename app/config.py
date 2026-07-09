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


@lru_cache
def get_settings() -> Settings:
    return Settings()
