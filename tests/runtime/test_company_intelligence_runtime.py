from datetime import datetime, timezone

from trader.domain.company import CompanyEvidenceItem, CompanyEvidenceSnapshot, CompanyIntelligenceBrief, IssuerIdentity
from trader.runtime.company_intelligence_runtime import CompanyIntelligenceRuntime, discover_company_symbols


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
        assert second["skipped"][0]["reason"] == "unchanged_evidence"
        assert runtime.brief_store.read_current("EXM") is not None
        assert runtime.status()["queue"]["done"] == 1
        assert written[0]["symbol"] == "EXM"
        assert runtime.ledger.path == state_dir / "company_research_tasks.db"
    finally:
        runtime.stop()


def test_discover_current_scope_keeps_candidates_sticky_and_active_universe(tmp_path) -> None:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    (config_dir / "universe.yaml").write_text("symbols: [ACTIVE]\n")
    from trader.infrastructure.state_db.candidate_scope_store import CandidateScopeStore

    CandidateScopeStore(state_dir / "candidate_scopes").append(
        {
            "candidate_scope_id": "scope-us",
            "venue": "US",
            "as_of": "2026-07-10T08:00:00+00:00",
            "candidates": [{"symbol": "CAND"}],
            "sticky_context_at_close": ["STICKY"],
        }
    )

    symbols, metadata = discover_company_symbols(config_dir=config_dir, state_dir=state_dir)

    assert symbols == ("CAND", "STICKY", "ACTIVE")
    assert metadata["candidate_scope_ids"] == ["scope-us"]
    assert metadata["fallback"] is False


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
