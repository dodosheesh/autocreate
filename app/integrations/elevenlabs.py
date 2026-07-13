"""ElevenLabs Voice Changer — endpoint speech-to-speech (brief §7).

Re-rend un audio dans une voix cible en préservant timing/émotion/pacing :
les lèvres (déjà synchro sur l'audio Seedance) restent synchro après swap.
Une voix cible par appel → l'appelant segmente et mappe tag → voice_id.
Limites API : 5 min / 50 Mo par appel (largement suffisant par segment).
"""

import json
from pathlib import Path

import httpx

from app.config import get_settings


class ElevenLabsError(RuntimeError):
    pass


def speech_to_speech(audio_path: str, voice_id: str, out_path: str) -> str:
    """Convertit `audio_path` dans la voix `voice_id`, écrit un mp3 44.1 kHz.
    Durée d'entrée préservée par le modèle sts."""
    settings = get_settings()
    if not settings.elevenlabs_api_key:
        raise ElevenLabsError("ELEVENLABS_API_KEY manquant")
    url = (
        f"{settings.elevenlabs_base_url.rstrip('/')}/v1/speech-to-speech/{voice_id}"
        "?output_format=mp3_44100_128"
    )
    with open(audio_path, "rb") as audio_file:
        resp = httpx.post(
            url,
            headers={"xi-api-key": settings.elevenlabs_api_key},
            data={
                "model_id": settings.elevenlabs_sts_model,
                "voice_settings": json.dumps(
                    {
                        "stability": settings.elevenlabs_stability,
                        "similarity_boost": settings.elevenlabs_similarity_boost,
                    }
                ),
            },
            files={"audio": (Path(audio_path).name, audio_file, "audio/wav")},
            timeout=300,
        )
    if resp.status_code != 200:
        raise ElevenLabsError(
            f"speech_to_speech {voice_id} → HTTP {resp.status_code} : {resp.text[:500]}"
        )
    with open(out_path, "wb") as f:
        f.write(resp.content)
    return out_path


def transcribe_audio(audio_path: str) -> str:
    """Transcrit un fichier audio en texte (ElevenLabs Scribe, speech-to-text).
    Renvoie le texte reconnu (chaîne vide si rien / pas de parole)."""
    settings = get_settings()
    if not settings.elevenlabs_api_key:
        raise ElevenLabsError("ELEVENLABS_API_KEY manquant")
    url = f"{settings.elevenlabs_base_url.rstrip('/')}/v1/speech-to-text"
    with open(audio_path, "rb") as audio_file:
        resp = httpx.post(
            url,
            headers={"xi-api-key": settings.elevenlabs_api_key},
            data={"model_id": settings.elevenlabs_stt_model},
            files={"file": (Path(audio_path).name, audio_file, "audio/wav")},
            timeout=300,
        )
    if resp.status_code != 200:
        raise ElevenLabsError(
            f"speech_to_text → HTTP {resp.status_code} : {resp.text[:500]}"
        )
    return (resp.json().get("text") or "").strip()
