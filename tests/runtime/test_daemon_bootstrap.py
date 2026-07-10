from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from trader.runtime import daemon_bootstrap


NOW = datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc)


class RecordingLogger:
    def __init__(self) -> None:
        self.infos: list[tuple] = []
        self.warnings: list[tuple] = []

    def info(self, *args: object) -> None:
        self.infos.append(args)

    def warning(self, *args: object) -> None:
        self.warnings.append(args)


def test_bootstrap_runtime_state_rotates_bootstraps_then_builds_scheduler(tmp_path: Path) -> None:
    calls: list[dict] = []
    scheduler = object()
    commission_model = object()
    logger = RecordingLogger()

    def rotate_monthly_fn(path: Path, archive_dir: Path, *, now: datetime, ts_key: str) -> dict:
        calls.append(
            {
                "step": "rotate",
                "path": path.name,
                "archive_dir": archive_dir,
                "now": now,
                "ts_key": ts_key,
            }
        )
        return {"archived": 2 if path.name == "decisions.jsonl" else 0, "kept": 5, "files": ["archive.gz"]}

    def load_starting_cash_fn(config_dir: Path) -> float:
        calls.append({"step": "load_cash", "config_dir": config_dir})
        return 123456.0

    def bootstrap_state_backend_fn(**kwargs: object) -> None:
        calls.append({"step": "bootstrap", "kwargs": kwargs})

    def make_scheduler_fn(**kwargs: object) -> object:
        calls.append({"step": "scheduler", "kwargs": kwargs})
        return scheduler

    result = daemon_bootstrap.bootstrap_runtime_state(
        state_dir=tmp_path / "state",
        config_dir=tmp_path / "config",
        commission_model=commission_model,
        now=NOW,
        logger=logger,
        rotate_monthly_fn=rotate_monthly_fn,
        load_starting_cash_fn=load_starting_cash_fn,
        bootstrap_state_backend_fn=bootstrap_state_backend_fn,
        make_scheduler_fn=make_scheduler_fn,
    )

    assert result.scheduler is scheduler
    assert result.archive_dir == tmp_path / "state" / "archive"
    assert [call["step"] for call in calls] == [
        "rotate",
        "rotate",
        "load_cash",
        "bootstrap",
        "scheduler",
    ]
    assert calls[0]["path"] == "decisions.jsonl"
    assert calls[0]["ts_key"] == "cycle_ts"
    assert calls[1]["path"] == "events.jsonl"
    assert calls[1]["ts_key"] == "ts"
    assert calls[3]["kwargs"] == {
        "state_dir": tmp_path / "state",
        "starting_cash": 123456.0,
        "commission_model": commission_model,
        "backend": "sqlite",
    }
    assert calls[4]["kwargs"] == {
        "state_dir": tmp_path / "state",
        "backend": "sqlite",
    }
    assert logger.infos == [
        (
            "[rotation] %s : archived=%d kept=%d files=%s",
            "decisions.jsonl",
            2,
            5,
            ["archive.gz"],
        )
    ]


def test_bootstrap_runtime_state_swallows_rotation_errors(tmp_path: Path) -> None:
    calls: list[str] = []
    logger = RecordingLogger()

    def rotate_monthly_fn(path: Path, _archive_dir: Path, *, now: datetime, ts_key: str) -> dict:
        calls.append(f"rotate:{path.name}:{ts_key}:{now.isoformat()}")
        raise RuntimeError("archive locked")

    result = daemon_bootstrap.bootstrap_runtime_state(
        state_dir=tmp_path / "state",
        config_dir=tmp_path / "config",
        commission_model=object(),
        now=NOW,
        logger=logger,
        rotate_monthly_fn=rotate_monthly_fn,
        load_starting_cash_fn=lambda _config_dir: 100000.0,
        bootstrap_state_backend_fn=lambda **_kwargs: calls.append("bootstrap"),
        make_scheduler_fn=lambda **_kwargs: "scheduler",
    )

    assert result.scheduler == "scheduler"
    assert calls == [
        "rotate:decisions.jsonl:cycle_ts:2026-07-05T10:00:00+00:00",
        "rotate:events.jsonl:ts:2026-07-05T10:00:00+00:00",
        "bootstrap",
    ]
    assert len(logger.warnings) == 2
    assert logger.warnings[0][0:2] == ("[rotation] échec sur %s : %s", "decisions.jsonl")
    assert logger.warnings[1][0:2] == ("[rotation] échec sur %s : %s", "events.jsonl")
    assert [str(warning[2]) for warning in logger.warnings] == ["archive locked", "archive locked"]
