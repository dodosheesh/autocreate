"""Reverse-engineering image/vidéo → prompt via un LLM vision.

kie.ai expose ses LLM (Gemini, GPT-4o…) derrière un endpoint
OpenAI-compatible (`/v1/chat/completions`). On envoie une ou plusieurs images
en messages `image_url` et on demande un prompt de génération réutilisable.
Le modèle et l'URL sont configurables (`KIE_VISION_MODEL`,
`KIE_VISION_BASE_URL`) : aucune valeur kie-spécifique n'est devinée en dur.
"""

import base64
import mimetypes
import re

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
    "character's face is supplied separately. Output only the prompt text, no preamble, no markdown."
)


# Reverse-engineering VIDÉO : on veut un TEMPLATE réutilisable qui transfère
# l'action / le mouvement de caméra / le rythme / l'ambiance de la vidéo de
# référence, mais PAS l'outfit, le décor ni le visage (remplacés par les assets
# et la model). Le template doit contenir les slots littéraux à remplir.
REVERSE_ENGINEER_VIDEO_SYSTEM = (
    "You are a prompt engineer for an image-to-video model. You receive ordered "
    "keyframes sampled from a reference video. Write ONE reusable video-generation "
    "TEMPLATE that captures the SUBJECT'S ACTION, CAMERA MOVEMENT, FRAMING, PACING, "
    "LIGHTING and MOOD of the clip, as a 9:16 vertical video. "
    "Do NOT describe the specific outfit, the specific location, or the person's "
    "facial identity — those are replaced. Refer to the subject generically as 'a woman'. "
    "The template MUST include these literal placeholder tokens where relevant: "
    "{outfit} for her clothing, {background} for the location, {characteristics} for her "
    "distinctive traits. Output ONLY the template text, no preamble, no markdown."
)


class VisionError(RuntimeError):
    pass


def _anthropic_content(content_parts: list[dict]) -> list[dict]:
    """Traduit les content-parts façon OpenAI (text / image_url) vers le format
    Messages d'Anthropic (text / image avec source url ou base64)."""
    out: list[dict] = []
    for part in content_parts:
        if part.get("type") == "text":
            out.append({"type": "text", "text": part.get("text", "")})
        elif part.get("type") == "image_url":
            url = part["image_url"]["url"]
            if url.startswith("data:"):
                header, _, b64 = url.partition(",")
                media_type = header[5:].split(";")[0] or "image/jpeg"
                out.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                })
            else:
                out.append({"type": "image", "source": {"type": "url", "url": url}})
    return out


def _call_anthropic(system: str, content_parts: list[dict], settings) -> str:
    resp = httpx.post(
        f"{settings.anthropic_base_url.rstrip('/')}/v1/messages",
        headers={
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": settings.anthropic_vision_model,
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": _anthropic_content(content_parts)}],
        },
        timeout=180,
    )
    if resp.status_code != 200:
        raise VisionError(f"Claude vision HTTP {resp.status_code} : {resp.text[:500]}")
    data = resp.json()
    try:
        blocks = data["content"]
    except (KeyError, TypeError) as exc:
        raise VisionError(f"Réponse Claude inattendue : {str(data)[:500]}") from exc
    text = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
    if not text:
        raise VisionError("Claude a renvoyé un texte vide")
    return text


def _call_vision(system: str, content_parts: list[dict]) -> str:
    settings = get_settings()
    # Claude prioritaire si une clé Anthropic est fournie ; sinon kie.ai/Gemini.
    if settings.anthropic_api_key:
        return _call_anthropic(system, content_parts, settings)
    if not settings.kie_api_key:
        raise VisionError("KIE_API_KEY manquant")
    body = {
        "model": settings.kie_vision_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content_parts},
        ],
    }
    resp = httpx.post(
        f"{settings.kie_vision_base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.kie_api_key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=180,
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


def _frame_data_uri(path: str) -> str:
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:{mime};base64,{b64}"


def _clean_generated_prompt(text: str) -> str:
    """Retire les artefacts markdown que certains modèles vision ajoutent en
    tête (« # Prompt », « **Prompt:** », fences ```), pour ne garder que le
    prompt exploitable."""
    text = (text or "").strip()
    # fences de code éventuels ```lang ... ```
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    # label « # Prompt », « ## Prompt: », « **Prompt:** », « Prompt: » en tête
    # (# / * / : / espaces autour du mot, dans n'importe quel ordre)
    text = re.sub(r"^[#*\s]*prompt[#*\s]*:?[#*\s]*", "", text, count=1, flags=re.IGNORECASE)
    # tout dièse d'en-tête résiduel en tout début
    text = re.sub(r"^\s*#{1,6}\s*", "", text)
    return text.strip()


def reverse_engineer_prompt(image_url: str, model_description: str | None = None) -> str:
    """Renvoie un prompt de génération décrivant l'image, adapté à la model.

    model_description : traits de la model injectés en contexte pour que le
    prompt colle à son style (le visage/les caractéristiques précises sont
    ajoutés comme images de référence à la génération, pas décrits ici)."""
    user_text = "Reverse-engineer this photo into a reusable generation prompt."
    if model_description:
        user_text += f" Adapt the wording so it fits this recurring model: {model_description}."
    return _clean_generated_prompt(
        _call_vision(
            REVERSE_ENGINEER_SYSTEM,
            [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        )
    )


# Auto-description d'assets (import bulk) : phrase courte, prête à insérer dans
# un prompt via le slot {outfit} / {background}.
_DESCRIBE_SYSTEM = {
    "outfit": (
        "Describe ONLY the clothing/outfit worn in this photo, as one short English "
        "phrase usable after 'wearing' (e.g. 'a red silk mini dress and black heels'). "
        "Do NOT describe the person's face, body or the background. Output only the phrase."
    ),
    "background": (
        "Describe ONLY the location/setting/background of this photo, as one short "
        "English phrase (e.g. 'a neon-lit city street at night'). Do NOT describe any "
        "person. Output only the phrase."
    ),
}


def describe_image(image_url: str, mode: str) -> str:
    """Renvoie une phrase courte décrivant l'outfit ou le background de l'image."""
    system = _DESCRIBE_SYSTEM.get(mode)
    if system is None:
        raise VisionError(f"mode de description inconnu : {mode!r}")
    text = _call_vision(
        system,
        [
            {"type": "text", "text": "Describe this image as instructed."},
            {"type": "image_url", "image_url": {"url": image_url}},
        ],
    )
    # garde une phrase propre, sans guillemets ni ponctuation finale parasite
    return text.strip().strip('"').strip().rstrip(".")


def reverse_engineer_video_prompt(
    frame_paths: list[str], model_description: str | None = None, speaking: bool = False
) -> str:
    """À partir des images-clés d'une vidéo, renvoie un TEMPLATE réutilisable
    (avec slots) transférant l'action/caméra/rythme à la model."""
    if not frame_paths:
        raise VisionError("Aucune frame fournie pour le reverse-engineering vidéo")
    user_text = (
        "These are ordered keyframes from a reference video. Reverse-engineer it into a "
        "reusable video-generation template as instructed."
    )
    if speaking:
        user_text += " The subject is talking, so also include the literal token {dialogue}."
    if model_description:
        user_text += f" Fit it to this recurring model: {model_description}."
    parts = [{"type": "text", "text": user_text}]
    for path in frame_paths:
        parts.append({"type": "image_url", "image_url": {"url": _frame_data_uri(path)}})
    return _clean_generated_prompt(_call_vision(REVERSE_ENGINEER_VIDEO_SYSTEM, parts))
