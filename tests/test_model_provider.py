import app.model_provider as model_provider
from app.model_provider import OpenAICompatibleProvider, _call_processing_engine, _day_start_utc, extract_json, estimate_tokens
from app.prompt_templates import render_prompt_template


def test_model_json_extraction():
    assert extract_json("```json\n{\"items\": []}\n```") == {"items": []}
    assert estimate_tokens("abcdef") >= 1


def test_day_start_utc_uses_shanghai_timezone():
    value = _day_start_utc()
    assert value.endswith("T16:00:00+00:00")


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


def test_codex_processing_engine_uses_template_and_only_sends_runtime_messages(monkeypatch):
    import app.codex_agent as codex_agent

    captured = {}

    def fake_codex(task, payload, schema, *, job_id):
        captured.update({"task": task, "payload": payload, "schema": schema, "job_id": job_id})
        return {"items": []}

    monkeypatch.setattr(model_provider, "get_setting", lambda key, default=None: "codex" if key == "processing_engine" else default)
    monkeypatch.setattr(model_provider, "record_model_usage", lambda result, task_type: None)
    monkeypatch.setattr(codex_agent, "run_codex_json", fake_codex)
    system_prompt = render_prompt_template("recruitment_extract")
    user_message = {"role": "user", "content": '{"messages":[]}'}

    result = _call_processing_engine(
        [{"role": "system", "content": system_prompt}, user_message],
        "recruitment_extract",
        {"type": "object"},
        job_id="job-1",
    )

    assert result.payload == {"items": []}
    assert captured == {
        "task": "recruitment_extract",
        "payload": {"messages": [user_message]},
        "schema": {"type": "object"},
        "job_id": "job-1",
    }
