from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_RUNTIME_INPUT_PLACEHOLDER = "{{RUNTIME_INPUT}}"
_PROMPT_FILES = {
    "recruitment_extract": "recruitment_extract.md",
    "recruitment_source_consolidation": "recruitment_source_consolidation.md",
    "company_consolidation": "company_consolidation.md",
    "company_merge_content": "company_merge_content.md",
    "historical_entity_dedup": "historical_entity_dedup.md",
    "source_text_extraction": "source_text_extraction.md",
    "company_public_research": "company_public_research.md",
    "connection_test": "connection_test.md",
}
PROMPT_TASKS = tuple(_PROMPT_FILES)


def prompt_template_path(task: str) -> Path:
    filename = _PROMPT_FILES.get(task)
    if not filename:
        raise ValueError(f"Unsupported prompt task: {task}")
    return Path(__file__).resolve().parent / "prompts" / filename


def load_prompt_template(task: str) -> str:
    path = prompt_template_path(task)
    try:
        template = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Prompt template could not be read: {path}") from exc
    if not template:
        raise RuntimeError(f"Prompt template is empty: {path}")
    if template.count(_RUNTIME_INPUT_PLACEHOLDER) != 1:
        raise RuntimeError(
            f"Prompt template must contain exactly one {_RUNTIME_INPUT_PLACEHOLDER} placeholder: {path}"
        )
    return template


def render_prompt_template(task: str, payload: dict[str, Any] | None = None) -> str:
    template = load_prompt_template(task)
    runtime_input = (
        json.dumps({"task": task, "input": payload}, ensure_ascii=False)
        if payload is not None
        else "运行时输入由调用方通过后续 user 消息提供。"
    )
    return template.replace(_RUNTIME_INPUT_PLACEHOLDER, runtime_input)
