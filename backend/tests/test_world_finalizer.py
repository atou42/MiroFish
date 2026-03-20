import json

from scripts.run_world_staged_experiment import generate_world_report_with_fallback
from scripts.world_run_diagnostics import validate_summary


def test_generate_world_report_with_fallback_reuses_same_report_id(monkeypatch):
    calls = []

    def fake_run_command(cmd, cwd, **kwargs):
        calls.append(cmd)
        if len(calls) == 1:
            return {
                "command": cmd,
                "cwd": cwd,
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "timed_out": True,
                "started_at": "t1",
                "completed_at": "t2",
                "duration_seconds": 10.0,
            }
        payload = {
            "simulation_id": "sim_x",
            "label": "final",
            "report_id": "report_world_sim_x_final",
            "report_status": "completed",
            "report_dir": "/tmp/report_world_sim_x_final",
            "report_json_path": "/tmp/report_world_sim_x_final/meta.json",
            "report_markdown_path": "/tmp/report_world_sim_x_final/full_report.md",
            "validation_errors": [],
            "generated_at": "now",
        }
        return {
            "command": cmd,
            "cwd": cwd,
            "returncode": 0,
            "stdout": json.dumps(payload, ensure_ascii=False),
            "stderr": "",
            "timed_out": False,
            "started_at": "t3",
            "completed_at": "t4",
            "duration_seconds": 1.0,
        }

    monkeypatch.setattr("scripts.run_world_staged_experiment._run_command", fake_run_command)
    monkeypatch.setattr("scripts.run_world_staged_experiment.reset_report_dir", lambda report_id: None)
    monkeypatch.setattr("scripts.run_world_staged_experiment.validate_report_artifacts", lambda report_id, report_dir: [])

    result = generate_world_report_with_fallback(
        python_bin="python",
        backend_dir="/tmp",
        simulation_id="sim_x",
        label="final",
        report_id="report_world_sim_x_final",
        timeout_seconds=5,
    )

    assert result["used_fallback"] is True
    assert len(result["attempts"]) == 2
    assert result["attempts"][0]["mode"] == "live"
    assert result["attempts"][1]["mode"] == "fallback_only"
    assert result["attempts"][0]["report_id"] == result["attempts"][1]["report_id"]
    assert "--report-id" in calls[0] and "--report-id" in calls[1]
    assert "report_world_sim_x_final" in calls[0]
    assert "report_world_sim_x_final" in calls[1]
    assert "--fallback-only" not in calls[0]
    assert "--fallback-only" in calls[1]


def test_validate_summary_requires_non_empty_artifacts():
    errors = validate_summary(
        {
            "json_path": "/missing/a.json",
            "markdown_path": "/missing/a.md",
            "actions_rows": 0,
            "checkpoint": {"status": "unknown", "last_completed_tick": 0},
            "diagnostics": {"last_tick_end": {}, "world_state": {}},
        }
    )

    assert "diagnostics json_path missing" in errors
    assert "actions_rows must be > 0" in errors
    assert any("checkpoint.status" in item for item in errors)
