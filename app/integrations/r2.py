"""Cloudflare R2 via l'API S3-compatible (boto3)."""

from functools import lru_cache

import boto3

from app.config import get_settings


@lru_cache
def _client():
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
    )


def upload_file(local_path: str, key: str, content_type: str = "video/mp4") -> str:
    """Upload et renvoie l'URL publique."""
    settings = get_settings()
    _client().upload_file(
        local_path,
        settings.r2_bucket,
        key,
        ExtraArgs={"ContentType": content_type},
    )
    return f"{settings.r2_public_base_url.rstrip('/')}/{key}"
