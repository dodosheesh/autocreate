"""Client kie.ai — API Market « Jobs ».

Réf : docs.kie.ai/market/bytedance/seedance-2
- POST /api/v1/jobs/createTask   {model, callBackUrl, input}
- GET  /api/v1/jobs/recordInfo?taskId=...
- États : waiting | success | fail
- resultJson (string JSON) contient {"resultUrls": ["...mp4"]}
- Le callback POSTé sur callBackUrl reprend la structure de recordInfo.
"""

import json
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings


class KieError(RuntimeError):
    pass


@dataclass(frozen=True)
class KieTaskResult:
    task_id: str
    state: str  # waiting | success | fail
    result_urls: list[str]
    fail_msg: str | None
    cost_time: float | None
    cost_usd: float | None  # coût réel si le payload l'expose (calibration)
    raw: dict


def _headers() -> dict[str, str]:
    settings = get_settings()
    if not settings.kie_api_key:
        raise KieError("KIE_API_KEY manquant")
    return {
        "Authorization": f"Bearer {settings.kie_api_key}",
        "Content-Type": "application/json",
    }


def build_seedance_input(
    prompt: str,
    reference_image_urls: list[str],
    resolution: str,
    duration_s: int,
    aspect_ratio: str = "9:16",
    generate_audio: bool = True,
) -> dict[str, Any]:
    return {
        "prompt": prompt,
        "reference_image_urls": reference_image_urls,
        "resolution": resolution,
        "duration": str(duration_s),
        "aspect_ratio": aspect_ratio,
        "generate_audio": generate_audio,
    }


def create_task(model: str, input_payload: dict[str, Any], callback_path: str = "/api/webhooks/kie") -> str:
    """Lance une tâche kie.ai (Market Jobs), renvoie le taskId.
    Générique : sert Seedance (vidéo) comme nano banana (image)."""
    settings = get_settings()
    callback_url = f"{settings.public_base_url.rstrip('/')}{callback_path}"
    if settings.kie_webhook_secret:
        callback_url += f"?secret={settings.kie_webhook_secret}"
    body = {"model": model, "callBackUrl": callback_url, "input": input_payload}
    resp = httpx.post(
        f"{settings.kie_base_url.rstrip('/')}/api/v1/jobs/createTask",
        headers=_headers(),
        json=body,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 200 or not data.get("data", {}).get("taskId"):
        raise KieError(f"createTask a échoué : {data}")
    return data["data"]["taskId"]


def create_seedance_task(input_payload: dict[str, Any], callback_path: str = "/api/webhooks/kie") -> str:
    return create_task(get_settings().kie_seedance_model, input_payload, callback_path)


def build_nano_banana_input(
    prompt: str,
    reference_image_urls: list[str],
    image_size: str = "1:1",
    output_format: str = "png",
) -> dict[str, Any]:
    """Input nano-banana-edit : édition guidée par images de référence
    (visage + caractéristiques + outfit) → consistance personnage."""
    return {
        "prompt": prompt,
        "image_urls": reference_image_urls,
        "image_size": image_size,
        "output_format": output_format,
    }


def create_nano_banana_task(input_payload: dict[str, Any], callback_path: str = "/api/webhooks/kie") -> str:
    return create_task(get_settings().kie_nano_banana_model, input_payload, callback_path)


def build_seedream_input(
    prompt: str,
    reference_image_urls: list[str],
    image_size: str = "1:1",
    resolution: str = "2K",
) -> dict[str, Any]:
    """Input Seedream 5.0 Pro image-to-image : édition multi-références (visage +
    caractéristiques). Clés attendues par ce modèle : `aspect_ratio` (et non
    image_size) + `quality` (1K/2K, et non image_resolution). image_urls ≤ 10."""
    return {
        "prompt": prompt,
        "image_urls": reference_image_urls,
        "aspect_ratio": image_size,
        "quality": resolution,
    }


def create_seedream_task(input_payload: dict[str, Any], callback_path: str = "/api/webhooks/kie") -> str:
    return create_task(get_settings().kie_seedream_model, input_payload, callback_path)


# Erreurs kie.ai/Google TRANSITOIRES (surcharge côté serveur) : un re-essai a de
# bonnes chances de passer. À distinguer des refus DÉFINITIFS (contenu sensible,
# input invalide) où re-tenter ne sert à rien.
_TRANSIENT_MARKERS = (
    "internal error",
    "try again later",
    "timeout",
    "timed out",
    "rate limit",
    "too many requests",
    "overload",
    "temporarily",
    "service unavailable",
    "503",
    "500",
)
_PERMANENT_MARKERS = (
    "sensitive",
    "flagged",
    "not supported",
    "invalid",
    "unsupported",
    "policy",
    "prohibited",
)


def is_transient_failure(fail_msg: str | None) -> bool:
    """True si l'échec kie.ai est probablement transitoire (re-essai utile)."""
    msg = (fail_msg or "").lower()
    if any(m in msg for m in _PERMANENT_MARKERS):
        return False
    return any(m in msg for m in _TRANSIENT_MARKERS)


def parse_task_payload(data: dict) -> KieTaskResult:
    """Parse le bloc `data` d'un recordInfo ou d'un callback (même structure)."""
    result_urls: list[str] = []
    result_json = data.get("resultJson")
    if result_json:
        try:
            parsed = json.loads(result_json) if isinstance(result_json, str) else result_json
            result_urls = parsed.get("resultUrls") or []
        except (json.JSONDecodeError, AttributeError):
            pass
    cost_usd = None
    for key in ("costUsd", "cost_usd", "costUSD", "cost"):
        value = data.get(key)
        if isinstance(value, (int, float)):
            cost_usd = float(value)
            break
    return KieTaskResult(
        task_id=data.get("taskId", ""),
        state=data.get("state", ""),
        result_urls=result_urls,
        fail_msg=data.get("failMsg"),
        cost_time=data.get("costTime"),
        cost_usd=cost_usd,
        raw=data,
    )


def get_task(task_id: str) -> KieTaskResult:
    """Fallback polling (les webhooks restent le chemin nominal)."""
    settings = get_settings()
    resp = httpx.get(
        f"{settings.kie_base_url.rstrip('/')}/api/v1/jobs/recordInfo",
        headers=_headers(),
        params={"taskId": task_id},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 200:
        raise KieError(f"recordInfo a échoué : {data}")
    return parse_task_payload(data.get("data", {}))
