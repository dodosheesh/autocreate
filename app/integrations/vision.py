"""Reverse-engineering image → prompt via un LLM vision.

kie.ai expose ses LLM (Gemini, GPT-4o…) derrière un endpoint
OpenAI-compatible (`/v1/chat/completions`). On envoie l'image en message
`image_url` et on demande un prompt de génération réutilisable. Le modèle
et l'URL sont configurables (`KIE_VISION_MODEL`, `KIE_VISION_BASE_URL`) :
aucune valeur kie-spécifique n'est devinée en dur.
"""

import httpx

from app.config import get_settings

# Consigne : décrire l'image comme un PROMPT de génération réutilisable,
# sans nommer de personne réelle ni copier un visage précis (le visage vient
# de la photo de référence de la model à la génération, pas du prompt).
REVERSE_ENGINEER_SYSTEM = (
    "You are a prompt engineer. Given a reference photo, write a single reusable "
    "image-generation prompt that captures its scene, composition, lighting, camera, "
    "pose, framing, mood and style. Describe the SUBJECT generically as 'the woman' — "
    "never identify or describe a specific real person's facial identity, since the "
    "character's face is supplied separately. Output only the prompt text, no preamble."
)


class VisionError(RuntimeError):
    pass


def reverse_engineer_prompt(image_url: str, model_description: str | None = None) -> str:
    """Renvoie un prompt de génération décrivant l'image, adapté à la model.

    model_description : traits de la model injectés en contexte pour que le
    prompt colle à son style (le visage/les caractéristiques précises sont
    ajoutés comme images de référence à la génération, pas décrits ici)."""
    settings = get_settings()
    if not settings.kie_api_key:
        raise VisionError("KIE_API_KEY manquant")
    user_text = "Reverse-engineer this photo into a reusable generation prompt."
    if model_description:
        user_text += (
            f" Adapt the wording so it fits this recurring model: {model_description}."
        )
    body = {
        "model": settings.kie_vision_model,
        "messages": [
            {"role": "system", "content": REVERSE_ENGINEER_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
    }
    resp = httpx.post(
        f"{settings.kie_vision_base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.kie_api_key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=120,
    )
    if resp.status_code != 200:
        raise VisionError(f"vision HTTP {resp.status_code} : {resp.text[:500]}")
    data = resp.json()
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise VisionError(f"Réponse vision inattendue : {str(data)[:500]}") from exc
    if isinstance(text, list):  # certains modèles renvoient une liste de parts
        text = " ".join(part.get("text", "") for part in text if isinstance(part, dict))
    text = (text or "").strip()
    if not text:
        raise VisionError("Le modèle vision a renvoyé un prompt vide")
    return text
