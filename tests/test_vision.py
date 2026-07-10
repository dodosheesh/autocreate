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
        kie_api_key="k", kie_vision_model="m", kie_vision_base_url="https://x/v1"
    )
    mock_post.return_value = _mock_response(500, {"error": "boom"})
    with pytest.raises(VisionError, match="500"):
        reverse_engineer_prompt("https://r2/img.jpg")


@patch("app.integrations.vision.get_settings")
def test_reverse_engineer_sans_cle(mock_settings):
    mock_settings.return_value = MagicMock(kie_api_key="")
    with pytest.raises(VisionError, match="KIE_API_KEY"):
        reverse_engineer_prompt("https://r2/img.jpg")
