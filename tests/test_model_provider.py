import app.model_provider as model_provider
from app.model_provider import OpenAICompatibleProvider, extract_json, estimate_tokens


def test_model_json_extraction():
    assert extract_json("```json\n{\"items\": []}\n```") == {"items": []}
    assert estimate_tokens("abcdef") >= 1


def test_openai_compatible_chat_response(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}

    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(model_provider.httpx, "post", fake_post)
    result = OpenAICompatibleProvider({"base_url": "https://api.example.com/v1", "model": "demo", "api_key": "secret", "name": "test"}).call([], "test")
    assert result.payload == {"ok": True}
    assert result.input_tokens == 7
    assert result.output_tokens == 3
    assert calls[0][0] == "https://api.example.com/v1/chat/completions"
