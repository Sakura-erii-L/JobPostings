from pathlib import Path

import pytest

import app.codex_agent as codex_agent
from app.prompt_templates import PROMPT_TASKS, render_prompt_template


EXPECTED_TASKS = {
    "recruitment_extract",
    "company_consolidation",
    "source_text_extraction",
    "company_public_research",
    "connection_test",
}
REQUIRED_GUIDANCE = {
    "recruitment_extract": (
        "message_id", "decision_reason", "matched_company_id", "major_requirements",
        "employment_type", "batch.year", "deadline", "start_at", "application_url",
    ),
    "company_consolidation": (
        "decision=normal", "decision=abnormal", "conflicts", "unsupported_claims", "summary",
    ),
    "source_text_extraction": ("岗位职责", "截止日期", "source_url", "notes"),
    "company_public_research": (
        "status=complete", "status=uncertain", "industry_codes", "negative_findings", "sources_checked",
    ),
    "connection_test": ("ok", "true", "JSON Schema"),
}


@pytest.mark.parametrize("task", sorted(EXPECTED_TASKS))
def test_each_codex_task_has_a_renderable_markdown_prompt(task):
    prompt = render_prompt_template(task, {"marker": f"payload-for-{task}"})

    assert "{{RUNTIME_INPUT}}" not in prompt
    assert f"payload-for-{task}" in prompt
    assert f'"task": "{task}"' in prompt
    assert "JSON Schema" in prompt


def test_prompt_task_registry_matches_supported_scenarios():
    assert set(PROMPT_TASKS) == EXPECTED_TASKS


@pytest.mark.parametrize("task", sorted(EXPECTED_TASKS))
def test_each_prompt_guides_codex_to_program_fields(task):
    prompt = render_prompt_template(task, {})

    for expected_text in REQUIRED_GUIDANCE[task]:
        assert expected_text in prompt


def test_generic_provider_prompt_replaces_runtime_placeholder():
    prompt = render_prompt_template("recruitment_extract")

    assert "{{RUNTIME_INPUT}}" not in prompt
    assert "运行时输入由调用方通过后续 user 消息提供。" in prompt


def test_unknown_prompt_task_is_rejected():
    with pytest.raises(ValueError, match="Unsupported prompt task"):
        render_prompt_template("unknown_task", {})


def test_run_codex_json_sends_rendered_markdown_prompt_to_stdin(monkeypatch):
    captured: dict[str, object] = {}

    class FakeProcess:
        returncode = 0

        def __init__(self, command, **kwargs):
            captured["command"] = command
            self.output_path = Path(command[command.index("--output-last-message") + 1])

        def communicate(self, prompt, timeout):
            captured["prompt"] = prompt
            captured["timeout"] = timeout
            self.output_path.write_text('{"ok": true}', encoding="utf-8")
            return "", ""

        def poll(self):
            return self.returncode

        def terminate(self):
            return None

    monkeypatch.setattr(codex_agent.Path, "home", lambda: Path("Z:/jobpostings-test-home-does-not-exist"))
    monkeypatch.setattr(codex_agent.shutil, "which", lambda _: "codex")
    monkeypatch.setattr(codex_agent.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(codex_agent, "_acquire_slot", lambda: None)
    monkeypatch.setattr(codex_agent, "_release_slot", lambda: None)

    result = codex_agent.run_codex_json(
        "connection_test",
        {"expected_output": {"ok": True}},
        {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        job_id="prompt-test",
    )

    assert result == {"ok": True}
    assert "# Codex 连接测试" in str(captured["prompt"])
    assert '"task": "connection_test"' in str(captured["prompt"])
    assert '"expected_output": {"ok": true}' in str(captured["prompt"])
