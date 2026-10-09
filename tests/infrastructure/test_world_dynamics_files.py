from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat

import pytest

from trader.domain.world_dynamics import ObservedDynamicsBar, SimulatedBar
from trader.domain.world_episode import AnchorBar
from trader.infrastructure.files import world_dynamics as files
from trader.infrastructure.state_db import shadow


BAR_T0 = datetime(2026, 10, 9, 6, tzinfo=timezone.utc)
CAPTURE_T0 = BAR_T0 + timedelta(days=20)


@dataclass
class _Clock:
    value: datetime = CAPTURE_T0 + timedelta(seconds=2)

    def __call__(self) -> datetime:
        return self.value


def _bar(index: int = 0, *, symbol: str = "2330.TW", first_seen_at: datetime = CAPTURE_T0) -> ObservedDynamicsBar:
    return ObservedDynamicsBar(
        venue="XTAI", symbol=symbol, bar_interval="1h",
        anchor=AnchorBar(
            ts=BAR_T0 + timedelta(hours=index), open=100, high=110, low=90,
            close=102, volume=1000, source="analysis_bars", timestamp_semantics="bar_close",
        ),
        first_seen_at=first_seen_at, recorded_at=first_seen_at,
    )


def _merge(journal: files.FilesystemDynamicsJournal, *bars: ObservedDynamicsBar, max_series: int = 4, max_bars: int = 256):
    return journal.merge(tuple(bars), max_series=max_series, max_bars_per_series=max_bars)


def test_journal_assigns_persistence_clock_and_preserves_first_receipt_across_retry_and_restart(tmp_path: Path) -> None:
    clock = _Clock()
    journal = files.FilesystemDynamicsJournal(tmp_path, clock=clock)
    candidate = _bar()
    initial = _merge(journal, candidate)
    assert initial.new_bars == 1
    first = initial.bars[0]
    assert first.first_seen_at == candidate.first_seen_at
    assert first.recorded_at == clock.value
    assert first.effective_available_at == clock.value
    assert first.payload_hash != candidate.payload_hash
    first_bytes = journal.path.read_bytes()

    clock.value += timedelta(hours=1)
    retried = _bar(first_seen_at=clock.value - timedelta(seconds=1))
    result = _merge(journal, retried)
    assert result.new_bars == 0
    assert result.duplicate_bars == 1
    assert result.conflicting_bars == 0
    assert result.bars == (first,)
    assert journal.path.read_bytes() == first_bytes

    restarted = files.FilesystemDynamicsJournal(tmp_path, clock=clock)
    restored = _merge(restarted)
    assert restored.bars == (first,)
    assert restored.new_bars == restored.duplicate_bars == 0
    assert restarted.path.read_bytes() == first_bytes


def test_journal_excludes_revisions_and_retains_original_payload_after_restart(tmp_path: Path) -> None:
    clock = _Clock()
    journal = files.FilesystemDynamicsJournal(tmp_path, clock=clock)
    original = _merge(journal, _bar()).bars[0]
    original_bytes = journal.path.read_bytes()
    clock.value += timedelta(minutes=1)
    revision = _bar(first_seen_at=clock.value - timedelta(seconds=1))
    revision = replace(revision, anchor=replace(revision.anchor, close=104))
    result = _merge(journal, revision, _bar(first_seen_at=clock.value))
    assert result.conflicting_bars == 1
    assert result.duplicate_bars == 1
    assert result.new_bars == 0
    assert result.bars == (original,)
    assert journal.path.read_bytes() == original_bytes
    assert _merge(files.FilesystemDynamicsJournal(tmp_path, clock=clock)).bars[0].payload_hash == original.payload_hash


@pytest.mark.parametrize("corruption", ["schema", "outer_hash", "evidence_hash", "geometry", "duplicate_slot", "invalid_json"])
def test_journal_corruption_fails_closed_and_is_never_overwritten(tmp_path: Path, corruption: str) -> None:
    journal = files.FilesystemDynamicsJournal(tmp_path, clock=_Clock())
    _merge(journal, _bar())
    payload = json.loads(journal.path.read_text())
    if corruption == "schema":
        payload["schema_version"] = "world_dynamics_journal.v0"
    elif corruption == "outer_hash":
        payload["bars"][0]["payload_hash"] = "incorrect"
    elif corruption == "evidence_hash":
        payload["bars"][0]["evidence"]["payload_hash"] = "incorrect"
    elif corruption == "geometry":
        payload["bars"][0]["evidence"]["anchor"]["close"] = 103
    elif corruption == "duplicate_slot":
        payload["bars"].append(payload["bars"][0])
    corrupted = "{invalid" if corruption == "invalid_json" else json.dumps(payload)
    journal.path.write_text(corrupted, encoding="utf-8")
    with pytest.raises(ValueError):
        _merge(journal, _bar(1))
    assert journal.path.read_text(encoding="utf-8") == corrupted


@pytest.mark.parametrize(
    ("max_series", "max_bars"),
    [(0, 256), (5, 256), (True, 256), (4, 1), (4, 257), (4, True)],
)
def test_journal_rejects_invalid_bounds_without_creating_state(tmp_path: Path, max_series: int, max_bars: int) -> None:
    journal = files.FilesystemDynamicsJournal(tmp_path, clock=_Clock())
    with pytest.raises(ValueError):
        _merge(journal, _bar(), max_series=max_series, max_bars=max_bars)
    assert not journal.path.exists()
    assert not journal.path.parent.exists()


def test_journal_accepts_four_times_256_bars_then_retains_latest_bars_per_pinned_scope(tmp_path: Path) -> None:
    journal = files.FilesystemDynamicsJournal(tmp_path, clock=_Clock())
    batch = tuple(_bar(index, symbol=f"S{series}.TW") for series in range(4) for index in range(256))
    initial = _merge(journal, *batch)
    assert len(initial.bars) == initial.new_bars == 1024
    assert initial.accepted_series == 4
    assert initial.evicted_bars == 0
    assert journal.path.stat().st_size < 4 * 1024 * 1024

    result = _merge(journal, _bar(256, symbol="S0.TW"), _bar(256, symbol="S1.TW"))
    assert result.new_bars == result.evicted_bars == 2
    assert len(result.bars) == 1024
    assert result.accepted_series == 4
    for symbol in ("S0.TW", "S1.TW"):
        series = [bar for bar in result.bars if bar.symbol == symbol]
        assert len(series) == 256
        assert series[0].anchor.ts == BAR_T0 + timedelta(hours=1)
        assert series[-1].anchor.ts == BAR_T0 + timedelta(hours=256)
    for symbol in ("S2.TW", "S3.TW"):
        assert [bar for bar in result.bars if bar.symbol == symbol] == [bar for bar in initial.bars if bar.symbol == symbol]
    restored = _merge(files.FilesystemDynamicsJournal(tmp_path, clock=_Clock()))
    assert restored.bars == result.bars


def test_journal_rejects_oversized_capture_batch_before_any_write(tmp_path: Path) -> None:
    journal = files.FilesystemDynamicsJournal(tmp_path, clock=_Clock())
    with pytest.raises(ValueError, match="capture batch exceeds"):
        _merge(journal, *(_bar(),) * 1025)
    assert not journal.path.exists()


def test_journal_pins_admitted_scopes_and_rejects_new_scope_without_evicting_old_scope(tmp_path: Path) -> None:
    journal = files.FilesystemDynamicsJournal(tmp_path, clock=_Clock())
    initial = _merge(journal, _bar(symbol="A.TW"), _bar(symbol="B.TW"), max_series=2, max_bars=2)
    result = _merge(
        journal, _bar(1, symbol="A.TW"), _bar(2, symbol="A.TW"),
        _bar(symbol="C.TW"), _bar(1, symbol="C.TW"), max_series=2, max_bars=2,
    )
    assert result.rejected_series == 1
    assert result.accepted_series == 2
    assert result.new_bars == 2
    assert result.evicted_bars == 1
    assert {bar.symbol for bar in result.bars} == {"A.TW", "B.TW"}
    assert [bar for bar in result.bars if bar.symbol == "B.TW"] == [bar for bar in initial.bars if bar.symbol == "B.TW"]
    restarted = files.FilesystemDynamicsJournal(tmp_path, clock=_Clock())
    rejected = _merge(restarted, _bar(symbol="C.TW"), max_series=2, max_bars=2)
    assert rejected.rejected_series == 1
    assert rejected.bars == result.bars


def test_journal_retention_configuration_is_pinned_after_initial_write(tmp_path: Path) -> None:
    journal = files.FilesystemDynamicsJournal(tmp_path, clock=_Clock())
    _merge(journal, _bar(), max_series=2, max_bars=2)
    original = journal.path.read_bytes()
    with pytest.raises(ValueError, match="retention differs"):
        _merge(journal, max_series=4, max_bars=2)
    with pytest.raises(ValueError, match="retention differs"):
        _merge(journal, max_series=2, max_bars=3)
    assert journal.path.read_bytes() == original


def test_journal_rejects_capture_clock_after_recording_and_simulated_bars(tmp_path: Path) -> None:
    journal = files.FilesystemDynamicsJournal(tmp_path, clock=_Clock())
    with pytest.raises(ValueError, match="capture clock follows"):
        _merge(journal, _bar(first_seen_at=CAPTURE_T0 + timedelta(seconds=3)))
    simulated = SimulatedBar(step_index=1, end_at=BAR_T0, open=100, high=100, low=100, close=100, volume=0)
    with pytest.raises(TypeError, match="real observed dynamics bars"):
        _merge(journal, simulated)
    assert not journal.path.exists()


def test_journal_refuses_oversized_existing_file_without_overwriting_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    journal = files.FilesystemDynamicsJournal(tmp_path, clock=_Clock())
    _merge(journal, _bar())
    original = journal.path.read_bytes()
    monkeypatch.setattr(files, "_MAX_JOURNAL_BYTES", len(original) - 1)
    with pytest.raises(ValueError, match="exceeds its byte limit"):
        _merge(journal, _bar(1))
    assert journal.path.read_bytes() == original


def test_journal_bounds_actual_pretty_serialization_before_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    initial = files.FilesystemDynamicsJournal(tmp_path / "initial", clock=_Clock())
    _merge(initial, _bar())
    payload = json.loads(initial.path.read_text())
    compact_bytes = len(json.dumps(payload).encode("utf-8"))
    actual_bytes = initial.path.stat().st_size
    assert compact_bytes < actual_bytes
    monkeypatch.setattr(files, "_MAX_JOURNAL_BYTES", (compact_bytes + actual_bytes) // 2)
    bounded = files.FilesystemDynamicsJournal(tmp_path / "bounded", clock=_Clock())
    with pytest.raises(ValueError, match="serialization exceeds"):
        _merge(bounded, _bar())
    assert not bounded.path.exists()


def test_journal_write_failure_leaves_original_evidence_and_no_partial_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    journal = files.FilesystemDynamicsJournal(tmp_path, clock=_Clock())
    original = _merge(journal, _bar()).bars
    original_bytes = journal.path.read_bytes()

    def fail_write(path: str, data: str, encoding: str = "utf-8") -> None:
        Path(path).write_text("{partial", encoding=encoding)
        raise OSError("injected disk failure")

    monkeypatch.setattr(shadow, "_write_text", fail_write)
    with pytest.raises(OSError, match="injected disk failure"):
        _merge(journal, _bar(1))
    assert journal.path.read_bytes() == original_bytes
    assert list(journal.path.parent.glob("*.tmp")) == []
    assert _merge(files.FilesystemDynamicsJournal(tmp_path, clock=_Clock())).bars == original


def test_journal_syncs_file_before_replace_and_all_new_directory_entries_before_return(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "new_state"
    journal = files.FilesystemDynamicsJournal(state_dir, clock=_Clock())
    events = []
    actual_fsync, actual_replace = shadow.os.fsync, shadow.os.replace

    def record_fsync(descriptor: int) -> None:
        kind = "directory" if stat.S_ISDIR(shadow.os.fstat(descriptor).st_mode) else "file"
        events.append(kind)
        actual_fsync(descriptor)

    def record_replace(source, target) -> None:
        events.append("replace")
        actual_replace(source, target)

    monkeypatch.setattr(shadow.os, "fsync", record_fsync)
    monkeypatch.setattr(shadow.os, "replace", record_replace)
    result = _merge(journal, _bar())
    events.append("evidence_returned")
    assert events == ["file", "replace", "directory", "directory", "directory", "evidence_returned"]
    assert result.new_bars == 1
    assert result.bars[0].recorded_at == CAPTURE_T0 + timedelta(seconds=2)


@pytest.mark.parametrize("failure_stage", ["file", "directory"])
def test_journal_sync_failure_never_returns_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str) -> None:
    journal = files.FilesystemDynamicsJournal(tmp_path, clock=_Clock())
    original = _merge(journal, _bar()).bars
    original_bytes = journal.path.read_bytes()
    actual_fsync = shadow.os.fsync

    def fail_fsync(descriptor: int) -> None:
        stage = "directory" if stat.S_ISDIR(shadow.os.fstat(descriptor).st_mode) else "file"
        if stage == failure_stage:
            raise OSError(f"injected {stage} sync failure")
        actual_fsync(descriptor)

    monkeypatch.setattr(shadow.os, "fsync", fail_fsync)
    returned = None
    with pytest.raises(OSError, match=f"injected {failure_stage} sync failure"):
        returned = _merge(journal, _bar(1))
    assert returned is None
    assert list(journal.path.parent.glob("*.tmp")) == []
    if failure_stage == "file":
        assert journal.path.read_bytes() == original_bytes
        assert _merge(files.FilesystemDynamicsJournal(tmp_path, clock=_Clock())).bars == original


def test_report_and_status_artifacts_use_stable_atomic_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = files.FilesystemDynamicsArtifacts(tmp_path)
    series = _bar().series_key
    path = Path(artifacts.write_report(series, {"version": 1, "authority": "shadow_only"}))
    assert path == tmp_path / "world_dynamics" / "reports" / f"{files.dynamics_series_id(series)}.json"
    second = artifacts.write_report(tuple(series), {"version": 2, "authority": "shadow_only"})
    assert second == str(path)
    assert json.loads(path.read_text()) == {"version": 2, "authority": "shadow_only"}
    assert list(artifacts.reports_dir.glob("*.json")) == [path]
    artifacts.write_status({"status": "ready", "last_report": str(path)})
    assert artifacts.status_path == tmp_path / "world_dynamics_status.json"
    assert json.loads(artifacts.status_path.read_text())["last_report"] == str(path)
    old_report, old_status = path.read_bytes(), artifacts.status_path.read_bytes()

    def fail_write(path: str, data: str, encoding: str = "utf-8") -> None:
        Path(path).write_text("{partial", encoding=encoding)
        raise OSError("injected artifact failure")

    monkeypatch.setattr(shadow, "_write_text", fail_write)
    with pytest.raises(OSError, match="injected artifact failure"):
        artifacts.write_report(series, {"version": 3})
    with pytest.raises(OSError, match="injected artifact failure"):
        artifacts.write_status({"status": "new"})
    assert path.read_bytes() == old_report
    assert artifacts.status_path.read_bytes() == old_status
    assert list(tmp_path.rglob("*.tmp")) == []
