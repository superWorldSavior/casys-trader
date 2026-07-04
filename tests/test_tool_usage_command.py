import json

from trader.commands import tool_usage


def test_tool_usage_command_writes_report_and_prints_json(monkeypatch, tmp_path, capsys) -> None:
    captured: dict = {}

    def fake_build_report(ledger, *, band, days_buffer, output_path):
        captured.update(
            {
                "ledger": ledger,
                "band": band,
                "days_buffer": days_buffer,
                "output_path": output_path,
            }
        )
        return {
            "ledger": str(ledger),
            "params": {"band": band, "days_buffer": days_buffer},
            "output_path": str(output_path),
        }

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tool_usage, "build_report", fake_build_report)

    tool_usage.main(
        [
            "--ledger",
            "custom.jsonl",
            "--band",
            "0.02",
            "--days-buffer",
            "3",
            "--json",
        ]
    )

    output_path = tmp_path / "state" / "last_tool_usage.json"
    assert captured == {
        "ledger": "custom.jsonl",
        "band": 0.02,
        "days_buffer": 3,
        "output_path": tool_usage.OUTPUT_PATH,
    }
    written = json.loads(output_path.read_text(encoding="utf-8"))
    printed = json.loads(capsys.readouterr().out)
    assert (
        written
        == printed
        == {
            "ledger": "custom.jsonl",
            "params": {"band": 0.02, "days_buffer": 3},
            "output_path": "state/last_tool_usage.json",
        }
    )
