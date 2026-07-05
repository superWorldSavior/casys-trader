from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from trader.runtime import cycle_finalization


class RecordingLogger:
    def __init__(self) -> None:
        self.debugs: list[tuple] = []
        self.infos: list[tuple] = []
        self.warnings: list[tuple] = []

    def debug(self, *args: object) -> None:
        self.debugs.append(args)

    def info(self, *args: object) -> None:
        self.infos.append(args)

    def warning(self, *args: object) -> None:
        self.warnings.append(args)


class FakeShadowProbe:
    def __init__(self, calls: list[dict]) -> None:
        self._calls = calls

    def run(
        self,
        *,
        cycle_ts: str,
        decidable_symbols: list[str],
        decided_symbols: list[str],
        now_ms: int,
    ) -> dict:
        self._calls.append(
            {
                "cycle_ts": cycle_ts,
                "decidable_symbols": decidable_symbols,
                "decided_symbols": decided_symbols,
                "now_ms": now_ms,
            }
        )
        return {
            "identical": False,
            "missing": ["MSFT"],
            "decided_vs_decidable": {"only_decidable": ["MSFT"]},
        }


def test_finalize_cycle_runs_runtime_side_effects_in_order(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    now = datetime(2026, 7, 5, 9, 30, tzinfo=timezone.utc)
    report = {
        "decisions": [
            {"symbol": "AAPL", "intent": "OPEN_LONG", "reason": "ok"},
            {"symbol": "MSFT", "intent": "HOLD", "reason": "gross_rejected: budget"},
        ],
    }
    raw_store = SimpleNamespace(path=state_dir / "learnings.jsonl")
    consolidated_store = SimpleNamespace(path=state_dir / "learnings_consolidated.json")
    learning_calls: list[dict] = []
    macro_calls: list[tuple[Path, datetime]] = []
    shadow_paths: list[Path] = []
    shadow_runs: list[dict] = []
    compare_calls: list[Path] = []
    write_calls: list[dict] = []
    events: list[tuple[str, dict]] = []
    cache: dict[str, dict | None] = {}
    logger = RecordingLogger()

    def consolidate(raw_store_arg: object, consolidated_store_arg: object, **kwargs: object) -> dict:
        learning_calls.append(
            {
                "raw_store": raw_store_arg,
                "consolidated_store": consolidated_store_arg,
                **kwargs,
            }
        )
        return {"triggered": True, "new_raw_count": 3, "summary": "ok"}

    def collect_macro(state_dir_arg: Path, now_arg: datetime) -> dict:
        macro_calls.append((state_dir_arg, now_arg))
        return {"triggered": True, "collected": 2, "skipped": 1, "errors": 0}

    def shadow_factory(db_path: Path) -> FakeShadowProbe:
        shadow_paths.append(db_path)
        return FakeShadowProbe(shadow_runs)

    def compare_state(state_dir_arg: Path) -> dict:
        compare_calls.append(state_dir_arg)
        return {
            "identical": False,
            "broker": {"cash": {"identical": True}, "positions_diff": ["AAPL"]},
            "trade_plans": {"diff": []},
            "scheduler": {"wakes_diff": [], "watches_diff": ["AAPL"], "stale_diff": []},
        }

    def summarize(decisions: list[dict]) -> dict | None:
        return {"symbols": [item["symbol"] for item in decisions]}

    cycle_finalization.finalize_cycle(
        state_dir=state_dir,
        now=now,
        report=report,
        decidable_symbols=["AAPL", "MSFT"],
        decided_symbols=["AAPL"],
        learning=cycle_finalization.LearningConsolidationRequest(
            raw_store=raw_store,
            consolidated_store=consolidated_store,
            threshold=50,
            acpx_bin="acpx",
            acpx_agent="codex",
            model="gpt-5.5/high",
            timeout_s=240,
            attribution={"n_closed_trades": 1},
            meta_performance={"samples": 4},
            consolidate=consolidate,
        ),
        gross_rejection_cache=cache,
        summarize_gross_rejections=summarize,
        collect_macro=collect_macro,
        shadow_queue_enabled=True,
        shadow_probe_factory=shadow_factory,
        state_backend="sqlite",
        compare_backends=compare_state,
        write_current_report=write_calls.append,
        append_event=lambda event, **payload: events.append((event, payload)),
        logger=logger,
    )

    assert learning_calls == [
        {
            "raw_store": raw_store,
            "consolidated_store": consolidated_store,
            "threshold": 50,
            "acpx_bin": "acpx",
            "acpx_agent": "codex",
            "model": "gpt-5.5/high",
            "timeout_s": 240,
            "attribution": {"n_closed_trades": 1},
            "meta_performance": {"samples": 4},
        }
    ]
    assert report["learning_consolidation"] == {"triggered": True, "new_raw_count": 3, "summary": "ok"}
    assert write_calls == [report]
    assert events == [("learning_consolidated", {"triggered": True, "new_raw_count": 3, "summary": "ok"})]
    assert macro_calls == [(state_dir, now)]
    assert cache == {str(state_dir): {"symbols": ["AAPL", "MSFT"]}}
    assert shadow_paths == [state_dir / "shadow_queue.db"]
    assert shadow_runs == [
        {
            "cycle_ts": "2026-07-05T09:30:00+00:00",
            "decidable_symbols": ["AAPL", "MSFT"],
            "decided_symbols": ["AAPL"],
            "now_ms": 1783243800000,
        }
    ]
    assert compare_calls == [state_dir]
    assert any(args[0] == "[state-compare] DIVERGENCE cycle=%s détail=%s" for args in logger.warnings)


def test_finalize_cycle_skips_report_write_when_learning_not_triggered(tmp_path: Path) -> None:
    report = {"decisions": []}
    writes: list[dict] = []
    events: list[tuple[str, dict]] = []

    cycle_finalization.finalize_cycle(
        state_dir=tmp_path,
        now=datetime(2026, 7, 5, tzinfo=timezone.utc),
        report=report,
        decidable_symbols=[],
        decided_symbols=[],
        learning=cycle_finalization.LearningConsolidationRequest(
            raw_store=object(),
            consolidated_store=object(),
            threshold=1,
            acpx_bin=None,
            acpx_agent=None,
            model=None,
            timeout_s=30,
            attribution=None,
            meta_performance=None,
            consolidate=lambda *_args, **_kwargs: {"triggered": False},
        ),
        gross_rejection_cache={},
        summarize_gross_rejections=lambda _decisions: None,
        collect_macro=lambda _state_dir, _now: {"triggered": False},
        shadow_queue_enabled=False,
        shadow_probe_factory=lambda _path: FakeShadowProbe([]),
        state_backend="json",
        compare_backends=lambda _state_dir: {"identical": True},
        write_current_report=writes.append,
        append_event=lambda event, **payload: events.append((event, payload)),
        logger=RecordingLogger(),
    )

    assert "learning_consolidation" not in report
    assert writes == []
    assert events == []


def test_finalize_cycle_keeps_optional_probes_best_effort(tmp_path: Path) -> None:
    logger = RecordingLogger()
    report = {"decisions": []}

    def raise_macro(_state_dir: Path, _now: datetime) -> dict:
        raise RuntimeError("macro down")

    def raise_shadow(_path: Path) -> FakeShadowProbe:
        raise RuntimeError("shadow down")

    def raise_compare(_state_dir: Path) -> dict:
        raise RuntimeError("compare down")

    cycle_finalization.finalize_cycle(
        state_dir=tmp_path,
        now=datetime(2026, 7, 5, tzinfo=timezone.utc),
        report=report,
        decidable_symbols=["AAPL"],
        decided_symbols=[],
        learning=None,
        gross_rejection_cache={},
        summarize_gross_rejections=lambda _decisions: None,
        collect_macro=raise_macro,
        shadow_queue_enabled=True,
        shadow_probe_factory=raise_shadow,
        state_backend="sqlite",
        compare_backends=raise_compare,
        write_current_report=lambda _report: None,
        append_event=lambda _event, **_payload: None,
        logger=logger,
    )

    messages = [args[0] for args in logger.warnings]
    assert "[shadow-queue] échec sonde: %s" in messages
    assert "[state-compare] échec: %s" in messages
