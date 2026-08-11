import json
from datetime import datetime, timedelta, timezone

import pytest

from trader.application.queue.contracts import RetryableError
from trader.domain.company import CompanyEvidenceItem, CompanyEvidenceSnapshot, CompanyIntelligenceBrief, IssuerIdentity
from trader.runtime.company_intelligence_runtime import (
    CompanyIntelligenceRuntime,
    decide_company_refresh,
    discover_company_symbols,
)


_CD_NOW = datetime(2026, 7, 10, 12, 30, tzinfo=timezone.utc)  # inside US pre-open (12:00-13:30)
_CD_COOLDOWN = timedelta(hours=24)
_CD_WINDOW = timedelta(minutes=90)


def test_decide_company_refresh_bootstrap_when_no_brief() -> None:
    assert decide_company_refresh(
        now=_CD_NOW, last_as_of=None, venue_in_preopen=False,
        cooldown=_CD_COOLDOWN, preopen_window=_CD_WINDOW,
    ) == (True, "bootstrap")


def test_decide_company_refresh_preopen_forces_once_then_reuses() -> None:
    stale = (_CD_NOW - timedelta(hours=3)).isoformat()
    assert decide_company_refresh(
        now=_CD_NOW, last_as_of=stale, venue_in_preopen=True,
        cooldown=_CD_COOLDOWN, preopen_window=_CD_WINDOW,
    ) == (True, "preopen")
    fresh = (_CD_NOW - timedelta(minutes=10)).isoformat()
    assert decide_company_refresh(
        now=_CD_NOW, last_as_of=fresh, venue_in_preopen=True,
        cooldown=_CD_COOLDOWN, preopen_window=_CD_WINDOW,
    ) == (False, "fresh")


def test_decide_company_refresh_cooldown_floor_outside_preopen() -> None:
    old = (_CD_NOW - timedelta(hours=25)).isoformat()
    assert decide_company_refresh(
        now=_CD_NOW, last_as_of=old, venue_in_preopen=False,
        cooldown=_CD_COOLDOWN, preopen_window=_CD_WINDOW,
    ) == (True, "cooldown")


def test_decide_company_refresh_reuses_when_fresh_and_not_preopen() -> None:
    recent = (_CD_NOW - timedelta(hours=2)).isoformat()
    assert decide_company_refresh(
        now=_CD_NOW, last_as_of=recent, venue_in_preopen=False,
        cooldown=_CD_COOLDOWN, preopen_window=_CD_WINDOW,
    ) == (False, "fresh")


def _evidence(symbol: str, as_of: str) -> CompanyEvidenceSnapshot:
    item = CompanyEvidenceItem.from_mapping(
        {
            "item_id": f"test:{symbol}:profile",
            "symbol": symbol,
            "provider": "test",
            "kind": "profile",
            "source_ref": f"test:{symbol}:profile",
            "source_name": "Test source",
            "as_of": as_of,
            "payload": {"issuer": symbol, "revenue": 100},
        }
    )
    assert item is not None
    return CompanyEvidenceSnapshot.build(
        symbol=symbol,
        as_of=as_of,
        identity=IssuerIdentity(f"{symbol} Corp", identity_status="verified"),
        items=(item,),
        coverage={"status": "partial"},
    )


class _Provider:
    def collect(self, *, symbol, as_of):
        return _evidence(symbol, as_of)


class _Analyst:
    def analyze(self, request):
        brief = CompanyIntelligenceBrief.from_mapping(
            {
                "symbol": request.symbol,
                "as_of": request.as_of,
                "input_signature": request.evidence.input_signature,
                "depth": request.depth,
                "issuer_identity": request.evidence.identity.to_dict(),
                "coverage": request.evidence.coverage,
                "business": {
                    "summary": "A business.",
                    "source_refs": [f"test:{request.symbol}:profile"],
                    "freshness": {"freshness": "fresh"},
                },
                "financial_snapshot": {"freshness": {"freshness": "missing"}},
                "earnings_and_guidance": {"freshness": {"freshness": "missing"}},
                "company_thesis": {"status": "untested", "summary": "Needs more evidence."},
                "selection_view": {
                    "posture": "insufficient_evidence",
                    "confidence": "low",
                    "reasons": ["Partial source coverage"],
                },
                "security_readiness": "not_decision_grade",
                "source_refs": [f"test:{request.symbol}:profile"],
            }
        )
        assert brief is not None
        return brief


def test_refresh_uses_dedicated_ledger_and_deduplicates_unchanged_evidence(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CASYS_COMPANY_MICRO_ANALYST_ENABLED", "1")
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    (config_dir / "company_intelligence.yaml").write_text("enabled: true\nresearch_concurrency: 1\n")
    written = []
    runtime = CompanyIntelligenceRuntime(
        config_dir=config_dir,
        state_dir=state_dir,
        provider=_Provider(),
        analyst=_Analyst(),
        concurrency=1,
        on_brief_written=written.append,
    )
    try:
        first = runtime.refresh(
            symbols=("EXM",),
            as_of=datetime(2026, 7, 10, 8, tzinfo=timezone.utc),
            wait=True,
            wait_timeout_s=2,
        )
        second = runtime.refresh(
            symbols=("EXM",),
            as_of=datetime(2026, 7, 10, 9, tzinfo=timezone.utc),
            wait=True,
            wait_timeout_s=2,
        )

        assert first["wait"]["completed"] is True
        assert first["wait"]["statuses"] == {str(first["enqueued"][0]["task_id"]): "done"}
        assert second["enqueued"] == []
        # 09:00 UTC: unchanged fundamentals, no venue in pre-open, within the
        # cooldown floor -> reuse the brief instead of re-analyzing.
        assert second["skipped"][0]["reason"] == "unchanged:fresh"
        assert runtime.brief_store.read_current("EXM") is not None
        assert runtime.status()["queue"]["done"] == 1
        assert written[0]["symbol"] == "EXM"
        assert runtime.ledger.path == state_dir / "company_research_tasks.db"
        assert runtime.ledger.get(first["enqueued"][0]["task_id"])["max_attempts"] == 6
        # The second pass is a scanner-only unchanged observation: its useful
        # status stays in ``skipped`` and does not add an attempt record.
        run_path = runtime.run_store.path_for_date("2026-07-10")
        assert len(run_path.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        runtime.stop()


def test_company_task_failure_preserves_last_success_until_a_real_recovery(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CASYS_COMPANY_MICRO_ANALYST_ENABLED", "1")
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    as_of = datetime(2026, 7, 10, 8, tzinfo=timezone.utc)
    runtime = CompanyIntelligenceRuntime(
        config_dir=config_dir,
        state_dir=state_dir,
        provider=_Provider(),
        analyst=_Analyst(),
        start_workers=False,
        now_fn=lambda: as_of.timestamp(),
    )
    evidence = _evidence("EXM", as_of.isoformat())

    def task(task_id: int, attempt: int) -> dict:
        return {
            "id": task_id,
            "attempts": attempt,
            "max_attempts": 6,
            "payload": json.dumps(
                {
                    "symbol": "EXM",
                    "as_of": as_of.isoformat(),
                    "depth": "screen",
                    "trigger": "scheduled",
                    "evidence": evidence.to_dict(),
                }
            ),
        }

    try:
        runtime._handle_task(task(1, 1), heartbeat=lambda: True)
        success = runtime.run_store.read_latest("EXM")
        assert success is not None
        assert success["status"] == "success"
        assert success["written"] is True

        class BrokenAnalyst:
            def analyze(self, request):
                raise RuntimeError("provider unavailable")

        runtime.analyst = BrokenAnalyst()
        with pytest.raises(RetryableError, match="RuntimeError: provider unavailable"):
            runtime._handle_task(task(2, 1), heartbeat=lambda: True)

        failed = runtime.run_store.read_latest("EXM")
        assert failed is not None
        assert failed["status"] == "success"
        assert failed["brief_ref"] == success["brief_ref"]
        assert failed["latest_failure"]["error_code"] == "RuntimeError"
        assert failed["latest_failure"]["retry_attempt"] == 1
        assert failed["latest_failure"]["retry_delay_seconds"] == 30 * 60
        status = runtime.status()
        assert status["latest_success"]["brief_ref"] == success["brief_ref"]
        assert status["latest_failure"]["error_code"] == "RuntimeError"

        # The repeated brief is not written again, but the completed analyst
        # call is still a real recovery and must clear latest_failure.
        runtime.analyst = _Analyst()
        assert json.loads(runtime._handle_task(task(3, 2), heartbeat=lambda: True))["status"] == "unchanged"
        recovered = runtime.run_store.read_latest("EXM")
        assert recovered is not None
        assert recovered["status"] == "success"
        assert recovered["written"] is False
        assert "latest_failure" not in recovered
    finally:
        runtime.stop()


def test_discover_current_scope_excludes_active_universe(tmp_path) -> None:
    """Screen = candidats/sticky HORS univers ; les membres d'univers (ici ACTIVE,
    et le candidat DUP qui est aussi dans l'univers) sont couverts par le deep."""
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    (config_dir / "universe.yaml").write_text("symbols: [ACTIVE, DUP]\n")
    from trader.infrastructure.state_db.candidate_scope_store import CandidateScopeStore

    CandidateScopeStore(state_dir / "candidate_scopes").append(
        {
            "candidate_scope_id": "scope-us",
            "venue": "US",
            "as_of": "2026-07-10T08:00:00+00:00",
            "candidates": [{"symbol": "CAND"}, {"symbol": "DUP"}],
            "sticky_context_at_close": ["STICKY"],
        }
    )

    symbols, metadata = discover_company_symbols(config_dir=config_dir, state_dir=state_dir)

    # ACTIVE (univers) et DUP (candidat AUSSI dans l'univers) sont exclus du screen.
    assert symbols == ("CAND", "STICKY")
    assert metadata["candidate_scope_ids"] == ["scope-us"]
    assert metadata["fallback"] is False
    assert metadata["screen_excludes_active"] is True


def test_discover_active_scope_returns_universe(tmp_path) -> None:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    (config_dir / "universe.yaml").write_text("symbols: [ACTIVE, DUP]\n")

    symbols, metadata = discover_company_symbols(
        config_dir=config_dir, state_dir=state_dir, scope="active"
    )

    assert set(symbols) == {"ACTIVE", "DUP"}
    assert metadata["scope"] == "active"


def test_status_only_runtime_does_not_start_queue_workers(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CASYS_COMPANY_MICRO_ANALYST_ENABLED", "1")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "company_intelligence.yaml").write_text("enabled: true\n")

    runtime = CompanyIntelligenceRuntime(
        config_dir=config_dir,
        state_dir=tmp_path / "state",
        provider=_Provider(),
        analyst=_Analyst(),
        start_workers=False,
    )
    try:
        assert runtime.status()["enabled"] is True
        assert runtime.pool._threads == []
    finally:
        runtime.stop()
