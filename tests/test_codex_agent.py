import json
from pathlib import Path
import subprocess

from app import codex_agent, processing


def test_codex_timeout_keeps_process_alive_for_late_result(tmp_path, monkeypatch):
    class FakeProcess:
        def __init__(self, command):
            self.command = command
            self.returncode = 0
            self.terminate_calls = 0
            self.communicate_calls = 0
            output_index = command.index("--output-last-message") + 1
            self.output_path = Path(command[output_index])

        def communicate(self, input=None, timeout=None):
            self.communicate_calls += 1
            if timeout is not None:
                raise subprocess.TimeoutExpired(self.command, timeout)
            self.output_path.write_text(json.dumps({"ok": True}), encoding="utf-8")
            return "", ""

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminate_calls += 1

    process_holder: dict[str, FakeProcess] = {}
    timeout_markers: list[tuple[str, str]] = []

    def fake_popen(command, **kwargs):
        process_holder["process"] = FakeProcess(command)
        return process_holder["process"]

    monkeypatch.setattr(codex_agent.config, "data_dir", tmp_path)
    monkeypatch.setattr(codex_agent.shutil, "which", lambda name: "codex.exe")
    monkeypatch.setattr(codex_agent.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(codex_agent, "_setting", lambda key, default: 1)
    monkeypatch.setattr(processing, "mark_processing_timeout", lambda job_id, error: timeout_markers.append((job_id, error)) or True)

    result = codex_agent.run_codex_json(
        "connection_test",
        {"expected_output": {"ok": True}},
        {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False},
        job_id="timeout-job",
        timeout_seconds=30,
    )

    assert result == {"ok": True}
    assert process_holder["process"].terminate_calls == 0
    assert process_holder["process"].communicate_calls == 2
    assert timeout_markers == [("timeout-job", "Local Codex timed out after 30 seconds")]


def test_explicit_cancel_force_termination_is_logged(monkeypatch):
    class StubbornProcess:
        def __init__(self):
            self.terminate_calls = 0
            self.kill_calls = 0
            self.wait_calls = 0

        def poll(self):
            return None

        def terminate(self):
            self.terminate_calls += 1

        def kill(self):
            self.kill_calls += 1

        def wait(self, timeout=None):
            self.wait_calls += 1
            raise subprocess.TimeoutExpired("codex", timeout)

    process = StubbornProcess()
    logs: list[tuple[str, str, str, str, dict]] = []
    monkeypatch.setattr(codex_agent, "_processes", {"manual-cancel": process})
    monkeypatch.setattr(
        processing,
        "log_processing",
        lambda job_id, stage, message, level="info", details=None: logs.append((job_id, stage, message, level, details or {})),
    )

    assert codex_agent.cancel_codex_job("manual-cancel", wait_seconds=0.1) is True
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == 2
    assert logs[-1][0] == "manual-cancel"
    assert logs[-1][3] == "error"
    assert logs[-1][4]["force_terminated"] is True
