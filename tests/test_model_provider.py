import json
import httpx
import app.model_provider as model_provider
import pytest
from app import db
from app.model_provider import OpenAICompatibleProvider, _call_processing_engine, _day_start_utc, consolidate_recruitment_source, create_usage_warning_notifications, extract_json, estimate_tokens
from app.prompt_templates import render_prompt_template


def test_model_json_extraction():
    assert extract_json("```json\n{\"items\": []}\n```") == {"items": []}
    assert estimate_tokens("abcdef") >= 1


def test_day_start_utc_uses_shanghai_timezone():
    value = _day_start_utc()
    assert value.endswith("T16:00:00+00:00")


def test_usage_warning_notifications_follow_levels_and_respect_daily_snooze(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    now = db.utc_now()
    with db.connect() as connection:
        connection.execute("INSERT INTO users(id,email,role,active,created_at) VALUES(?,?,?,?,?)", ("admin-1", "one@example.com", "admin", 1, now))
        connection.execute("INSERT INTO users(id,email,role,active,created_at) VALUES(?,?,?,?,?)", ("admin-2", "two@example.com", "admin", 1, now))
        connection.execute("UPDATE system_settings SET value_json=? WHERE key='llm_input_budget'", ("100",))
        connection.execute("UPDATE system_settings SET value_json=? WHERE key='llm_output_budget'", ("100000",))
        connection.execute(
            "INSERT INTO notifications(id,user_id,kind,title,body,created_at) VALUES(?,?,?,?,?,?)",
            ("snooze-1", "admin-2", "usage_warning_snooze", "今日不再提醒", "usage_warning", now),
        )
        connection.execute(
            "INSERT INTO llm_calls(id,provider_name,model_name,task_type,input_tokens,output_tokens,estimated,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("call-1", "test", "test", "test", 80, 0, 1, "succeeded", now),
        )

    assert create_usage_warning_notifications() == 1
    for level in (85, 90, 95, 100):
        with db.connect() as connection:
            connection.execute("UPDATE llm_calls SET input_tokens=? WHERE id='call-1'", (level,))
        assert create_usage_warning_notifications() == 1

    rows = db.all_rows("SELECT user_id,title FROM notifications WHERE kind LIKE 'usage_warning_%' AND kind<>'usage_warning_snooze' ORDER BY title")
    assert [(row["user_id"], row["title"]) for row in rows] == [
        ("admin-1", "模型额度已达到 100%"),
        ("admin-1", "模型额度已达到 80%"),
        ("admin-1", "模型额度已达到 85%"),
        ("admin-1", "模型额度已达到 90%"),
        ("admin-1", "模型额度已达到 95%"),
    ]


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


def test_openai_compatible_timeout_is_normalized(monkeypatch):
    def fake_post(url, **kwargs):
        raise httpx.TimeoutException("upstream timed out")

    monkeypatch.setattr(model_provider.httpx, "post", fake_post)
    provider = OpenAICompatibleProvider({"base_url": "https://api.example.com/v1", "model": "demo", "timeout_seconds": 3})
    with pytest.raises(TimeoutError, match="Generic LLM timed out after 3 seconds"):
        provider.call([], "test")


def test_generic_llm_processing_engine_uses_shared_validation(monkeypatch):
    captured = {}

    def fake_model(messages, task_type):
        captured.update({"messages": messages, "task_type": task_type})
        return model_provider.ModelResult({}, 1, 1, True, "generic", "demo")

    monkeypatch.setattr(model_provider, "get_setting", lambda key, default=None: "generic_llm" if key == "processing_engine" else default)
    monkeypatch.setattr(model_provider, "_call_model", fake_model)
    result = _call_processing_engine(
        [{"role": "system", "content": "system"}, {"role": "user", "content": "user"}],
        "test",
        {"type": "object", "required": [], "properties": {}, "additionalProperties": False},
        job_id="job-generic",
    )

    assert result.payload == {}
    assert result.provider == "generic"
    assert captured["task_type"] == "test"
    assert captured["messages"][0]["role"] == "system"


def test_source_consolidation_sends_complete_source_and_safe_metadata(monkeypatch):
    captured = {}

    def fake_model(messages, task_type):
        captured.update({"messages": messages, "task_type": task_type})
        return model_provider.ModelResult({"is_recruitment": False, "decision_reason": "非招聘", "companies": []}, 1, 1, True, "generic", "demo")

    monkeypatch.setattr(model_provider, "get_setting", lambda key, default=None: "generic_llm" if key == "processing_engine" else default)
    monkeypatch.setattr(model_provider, "_call_model", fake_model)
    source = "完整原文\n" + ("招聘正文 " * 100)
    consolidate_recruitment_source(
        source,
        {"is_recruitment": True, "companies": [{"company": {"display_name": "候选企业"}}]},
        {"title": "招聘汇总", "source_url": "https://example.com/source", "senderId": "must-not-leak"},
        "job-consolidate",
    )

    prompt = json.loads(captured["messages"][1]["content"])
    assert captured["task_type"] == "recruitment_source_consolidation"
    assert prompt["source_text"] == source
    assert prompt["structured_candidates"]["companies"][0]["company"]["display_name"] == "候选企业"
    assert prompt["source_metadata"] == {"title": "招聘汇总", "source_url": "https://example.com/source"}


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
