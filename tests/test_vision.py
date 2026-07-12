"""Vérifie la forme de la requête vision (endpoint OpenAI-compatible) sans
appel réseau réel : httpx.post est mocké."""

from unittest.mock import MagicMock, patch

import pytest

from app.integrations.vision import VisionError, reverse_engineer_prompt


def _mock_response(status=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_body or {}
    resp.text = str(json_body)
    return resp


@patch("app.integrations.vision.get_settings")
@patch("app.integrations.vision.httpx.post")
def test_reverse_engineer_construit_le_message_image(mock_post, mock_settings):
    mock_settings.return_value = MagicMock(
        anthropic_api_key="",
        kie_api_key="k",
        kie_vision_model="google/gemini-3-pro",
        kie_vision_base_url="https://api.kie.ai/gemini-3-pro/v1",
    )
    mock_post.return_value = _mock_response(
        200, {"choices": [{"message": {"content": "a cinematic portrait, soft light"}}]}
    )
    out = reverse_engineer_prompt("https://r2/img.jpg", model_description="tall brunette")
    assert out == "a cinematic portrait, soft light"

    _, kwargs = mock_post.call_args
    body = kwargs["json"]
    assert body["model"] == "google/gemini-3-pro"
    content = body["messages"][1]["content"]
    assert any(p["type"] == "image_url" and p["image_url"]["url"] == "https://r2/img.jpg" for p in content)
    assert any("tall brunette" in p.get("text", "") for p in content)
    assert mock_post.call_args[0][0].endswith("/chat/completions")


@patch("app.integrations.vision.get_settings")
@patch("app.integrations.vision.httpx.post")
def test_reverse_engineer_erreur_http(mock_post, mock_settings):
    mock_settings.return_value = MagicMock(
        anthropic_api_key="", kie_api_key="k", kie_vision_model="m", kie_vision_base_url="https://x/v1"
    )
    mock_post.return_value = _mock_response(500, {"error": "boom"})
    with pytest.raises(VisionError, match="500"):
        reverse_engineer_prompt("https://r2/img.jpg")


@patch("app.integrations.vision.get_settings")
def test_reverse_engineer_sans_cle(mock_settings):
    mock_settings.return_value = MagicMock(anthropic_api_key="", kie_api_key="")
    with pytest.raises(VisionError, match="KIE_API_KEY"):
        reverse_engineer_prompt("https://r2/img.jpg")


@patch("app.integrations.vision.get_settings")
@patch("app.integrations.vision.httpx.post")
def test_vision_route_vers_claude_si_cle_anthropic(mock_post, mock_settings):
    # Une clé Anthropic présente → l'analyse passe par Claude (Messages API),
    # pas par kie.ai. On vérifie l'endpoint, le format image et la réponse.
    mock_settings.return_value = MagicMock(
        anthropic_api_key="sk-ant-x",
        anthropic_base_url="https://api.anthropic.com",
        anthropic_vision_model="claude-haiku-4-5-20251001",
    )
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"content": [{"type": "text", "text": "a moody neon portrait"}]}
    mock_post.return_value = resp

    from app.integrations.vision import describe_image
    out = describe_image("https://r2/img.jpg", "background")
    assert out == "a moody neon portrait"

    url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
    assert url.endswith("/v1/messages")
    assert kwargs["headers"]["x-api-key"] == "sk-ant-x"
    body = kwargs["json"]
    assert body["model"] == "claude-haiku-4-5-20251001"
    content = body["messages"][0]["content"]
    assert any(p["type"] == "image" and p["source"]["type"] == "url"
               and p["source"]["url"] == "https://r2/img.jpg" for p in content)


def test_clean_generated_prompt_strip_markdown():
    from app.integrations.vision import _clean_generated_prompt
    assert _clean_generated_prompt("# Prompt\nThe woman stands in a store.") == "The woman stands in a store."
    assert _clean_generated_prompt("**Prompt:** A neon street") == "A neon street"
    assert _clean_generated_prompt("## Prompt: hello") == "hello"
    assert _clean_generated_prompt("```\nA plain prompt\n```") == "A plain prompt"
    assert _clean_generated_prompt("A clean prompt already") == "A clean prompt already"


@patch("app.integrations.vision.get_settings")
@patch("app.integrations.vision.httpx.post")
def test_reverse_engineer_nettoie_le_prefixe(mock_post, mock_settings):
    mock_settings.return_value = MagicMock(anthropic_api_key="", kie_api_key="k",
                                           kie_vision_model="m", kie_vision_base_url="https://x/v1")
    mock_post.return_value = _mock_response(
        200, {"choices": [{"message": {"content": "# Prompt\nA soft portrait, window light"}}]}
    )
    assert reverse_engineer_prompt("https://r2/img.jpg") == "A soft portrait, window light"


def test_anthropic_content_traduit_data_uri():
    from app.integrations.vision import _anthropic_content
    parts = [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
    ]
    out = _anthropic_content(parts)
    assert out[0] == {"type": "text", "text": "hi"}
    assert out[1]["source"] == {"type": "base64", "media_type": "image/png", "data": "QUJD"}
