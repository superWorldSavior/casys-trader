from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

from trader.domain.company import CompanyIntelligenceBrief
from trader.domain.situation import NewsMacroBrief
from trader.domain.world_context import NO_PROVEN_ARTIFACT_REASON
from trader.infrastructure.state_db.availability_receipt import (
    AVAILABILITY_RECEIPT_SCHEMA,
    payload_sha256,
)
from trader.infrastructure.state_db.company_intelligence_store import CompanyIntelligenceStore
from trader.infrastructure.state_db.situation_brief_store import NewsMacroBriefStore
from trader.infrastructure.state_db.world_context_reader import WorldContextReader


CUTOFF = datetime(2026, 8, 22, 10, 5, tzinfo=timezone.utc)
BOOT = datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc)


def _clock_at(moment: datetime):
    return lambda: moment


def _macro(point: str = "ECB watch") -> NewsMacroBrief:
    brief = NewsMacroBrief.from_mapping(
        {
            "brief_id": "2026-08-22|XTAI",
            "venue": "XTAI",
            "as_of": "2026-08-22T09:00:00+00:00",
            "valid_until": "2026-08-23T09:00:00+00:00",
            "zones": {"TW": [{"point": point, "sources": ["src-1"]}]},
        }
    )
    assert brief is not None
    return brief


def _company() -> CompanyIntelligenceBrief:
    brief = CompanyIntelligenceBrief.from_mapping(
        {
            "symbol": "AAA",
            "as_of": "2026-08-22T08:00:00+00:00",
            "input_signature": "sig-aaa",
            "depth": "screen",
            "issuer_identity": {"issuer_name": "AAA Ltd", "identity_status": "unverified"},
            "coverage": {"status": "partial"},
            "company_thesis": {"status": "intact"},
            "source_refs": ["src-a", "src-b"],
        }
    )
    assert brief is not None
    return brief


def test_public_append_does_not_accept_backdating_clocks() -> None:
    assert "recorded_at" not in inspect.signature(NewsMacroBriefStore.append).parameters
    assert "ready_at" not in inspect.signature(NewsMacroBriefStore.append).parameters
    assert "recorded_at" not in inspect.signature(CompanyIntelligenceStore.append).parameters
    assert "ready_at" not in inspect.signature(CompanyIntelligenceStore.append).parameters


def test_raw_history_stays_canonical_and_sidecar_is_stamped_after_append(tmp_path: Path) -> None:
    ready = datetime(2026, 8, 22, 9, 30, tzinfo=timezone.utc)
    seen: dict[str, str] = {}

    def news_clock() -> datetime:
        seen["news"] = (tmp_path / "news_briefs" / "2026-08-22.jsonl").read_text(encoding="utf-8")
        return ready

    def company_clock() -> datetime:
        seen["company"] = companies.history_path("AAA").read_text(encoding="utf-8")
        return ready

    news = NewsMacroBriefStore(tmp_path / "news_briefs", clock=news_clock)
    companies = CompanyIntelligenceStore(tmp_path / "company_intelligence", clock=company_clock)
    news.append(_macro())
    companies.append(_company())

    news_row = json.loads((tmp_path / "news_briefs" / "2026-08-22.jsonl").read_text().splitlines()[0])
    company_row = json.loads(companies.history_path("AAA").read_text().splitlines()[0])
    assert news_row["venue"] == "XTAI"
    assert "payload" not in news_row
    assert company_row["symbol"] == "AAA"
    assert "payload" not in company_row
    assert AVAILABILITY_RECEIPT_SCHEMA not in seen["news"]
    assert '"venue": "XTAI"' in seen["news"] or '"venue":"XTAI"' in seen["news"]
    receipt = json.loads(
        (tmp_path / "news_briefs" / "availability_receipts" / "2026-08-22.jsonl").read_text().splitlines()[0]
    )
    assert receipt["schema_version"] == AVAILABILITY_RECEIPT_SCHEMA
    assert receipt["ready_at"] == ready.isoformat()
    assert receipt["payload_sha256"] == payload_sha256(news_row)
    assert news.read_latest("XTAI").venue == "XTAI"
    assert companies.read_current("AAA").symbol == "AAA"
    assert companies.read_history("AAA")[0].company_thesis.status == "intact"


def test_legacy_rows_are_unproven_and_never_eligible(tmp_path: Path) -> None:
    news_dir = tmp_path / "news_briefs"
    news_dir.mkdir()
    (news_dir / "2026-08-22.jsonl").write_text(
        json.dumps(_macro().to_dict(), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    reader = WorldContextReader(news_dir=news_dir, company_dir=tmp_path / "company", clock=_clock_at(BOOT))
    evidence = reader.lookup_macro(venue="XTAI", cutoff_at=CUTOFF)
    assert evidence.status == "missing"
    assert evidence.reason == NO_PROVEN_ARTIFACT_REASON
    assert evidence.proven is False
    assert evidence.payload is None
    assert evidence.artifact is None


def test_tampered_payload_digest_and_ref_fail_closed(tmp_path: Path) -> None:
    news = NewsMacroBriefStore(
        tmp_path / "news_briefs",
        clock=lambda: datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc),
    )
    news.append(_macro())
    history = tmp_path / "news_briefs" / "2026-08-22.jsonl"
    row = json.loads(history.read_text().splitlines()[0])
    row["zones"]["TW"][0]["point"] = "tampered"
    history.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    reader = WorldContextReader(news_store=news, company_dir=tmp_path / "company", clock=_clock_at(BOOT))
    unproven = reader.lookup_macro(venue="XTAI", cutoff_at=CUTOFF)
    assert unproven.status == "missing"
    assert unproven.reason == NO_PROVEN_ARTIFACT_REASON
    assert unproven.proven is False

    news2 = NewsMacroBriefStore(
        tmp_path / "news2",
        clock=lambda: datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc),
    )
    news2.append(_macro("second"))
    receipt_path = tmp_path / "news2" / "availability_receipts" / "2026-08-22.jsonl"
    receipt = json.loads(receipt_path.read_text().splitlines()[0])
    receipt["payload_sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    reader2 = WorldContextReader(news_store=news2, company_dir=tmp_path / "company", clock=_clock_at(BOOT))
    digest_mismatch = reader2.lookup_macro(venue="XTAI", cutoff_at=CUTOFF)
    assert digest_mismatch.status == "missing"
    assert digest_mismatch.reason == NO_PROVEN_ARTIFACT_REASON

    news3 = NewsMacroBriefStore(
        tmp_path / "news3",
        clock=lambda: datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc),
    )
    news3.append(_macro("third"))
    receipt_path = tmp_path / "news3" / "availability_receipts" / "2026-08-22.jsonl"
    receipt = json.loads(receipt_path.read_text().splitlines()[0])
    receipt["artifact_id"] = "forged"
    receipt["artifact_ref"]["brief_id"] = "forged"
    receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    reader3 = WorldContextReader(news_store=news3, company_dir=tmp_path / "company", clock=_clock_at(BOOT))
    forged = reader3.lookup_macro(venue="XTAI", cutoff_at=CUTOFF)
    assert forged.status == "missing"
    assert forged.reason == NO_PROVEN_ARTIFACT_REASON


def test_late_and_stale_receipts_are_explicit(tmp_path: Path) -> None:
    news = NewsMacroBriefStore(
        tmp_path / "news_briefs",
        clock=lambda: datetime(2026, 8, 22, 11, 0, tzinfo=timezone.utc),
    )
    news.append(_macro())
    reader = WorldContextReader(
        news_store=news,
        company_dir=tmp_path / "company",
        clock=_clock_at(datetime(2026, 8, 22, 11, 0, tzinfo=timezone.utc)),
    )
    late = reader.lookup_macro(venue="XTAI", cutoff_at=CUTOFF)
    assert late.status == "missing"
    assert late.reason == NO_PROVEN_ARTIFACT_REASON
    assert late.proven is False
    assert late.payload is None
    assert late.artifact is None
    later = reader.lookup_macro(venue="XTAI", cutoff_at=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc))
    assert later.status == "complete"
    assert later.proven is True
    assert later.artifact is not None
    assert later.artifact.published_at is None

    stale_store = NewsMacroBriefStore(
        tmp_path / "stale",
        clock=lambda: datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc),
    )
    stale_brief = NewsMacroBrief.from_mapping(
        {
            "brief_id": "stale|XTAI",
            "venue": "XTAI",
            "as_of": "2026-08-21T09:00:00+00:00",
            "valid_until": "2026-08-22T10:00:00+00:00",
            "zones": {"TW": [{"point": "old", "sources": ["src-1"]}]},
        }
    )
    assert stale_brief is not None
    stale_store.append(stale_brief)
    stale_reader = WorldContextReader(news_store=stale_store, company_dir=tmp_path / "company", clock=_clock_at(BOOT))
    stale = stale_reader.lookup_macro(venue="XTAI", cutoff_at=CUTOFF)
    assert stale.status == "stale"
    assert stale.proven is True
    assert stale.artifact is not None
    assert stale.artifact.valid_until is not None
    assert stale.artifact.valid_until <= CUTOFF


def test_reader_never_uses_as_of_or_mtime_as_readiness(tmp_path: Path) -> None:
    news_dir = tmp_path / "news_briefs"
    news_dir.mkdir()
    payload = _macro().to_dict()
    payload["as_of"] = "2026-08-21T00:00:00+00:00"
    (news_dir / "2026-08-22.jsonl").write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    reader = WorldContextReader(news_dir=news_dir, company_dir=tmp_path / "missing", clock=_clock_at(BOOT))
    evidence = reader.lookup_macro(venue="XTAI", cutoff_at=CUTOFF)
    assert evidence.status == "missing"
    assert evidence.reason == NO_PROVEN_ARTIFACT_REASON
    assert evidence.proven is False


def test_eligible_company_receipt_is_complete(tmp_path: Path) -> None:
    companies = CompanyIntelligenceStore(
        tmp_path / "company_intelligence",
        clock=lambda: datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc),
    )
    companies.append(_company())
    reader = WorldContextReader(
        news_dir=tmp_path / "news",
        company_store=companies,
        clock=_clock_at(BOOT),
    )
    evidence = reader.lookup_company(symbol="AAA", cutoff_at=CUTOFF)
    assert evidence.status == "complete"
    assert evidence.artifact is not None
    assert evidence.artifact.ready_at <= CUTOFF
    assert evidence.artifact.published_at is None


def test_nested_history_envelope_is_not_readiness_proof(tmp_path: Path) -> None:
    news_dir = tmp_path / "news_briefs"
    news_dir.mkdir()
    payload = _macro().to_dict()
    envelope = {
        "schema_version": AVAILABILITY_RECEIPT_SCHEMA,
        "ready_at": "2026-08-22T09:00:00+00:00",
        "payload_sha256": payload_sha256(payload),
        "payload": payload,
    }
    (news_dir / "2026-08-22.jsonl").write_text(json.dumps(envelope, sort_keys=True) + "\n", encoding="utf-8")
    reader = WorldContextReader(news_dir=news_dir, company_dir=tmp_path / "company", clock=_clock_at(BOOT))
    nested = reader.lookup_macro(venue="XTAI", cutoff_at=CUTOFF)
    assert nested.status == "missing"
    assert nested.reason == NO_PROVEN_ARTIFACT_REASON


def test_malformed_valid_until_fails_closed_instead_of_unbounded(tmp_path: Path) -> None:
    news = NewsMacroBriefStore(
        tmp_path / "news_briefs",
        clock=lambda: datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc),
    )
    news.append(_macro())
    history = tmp_path / "news_briefs" / "2026-08-22.jsonl"
    row = json.loads(history.read_text().splitlines()[0])
    row["valid_until"] = "not-a-timestamp"
    history.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    receipt_path = tmp_path / "news_briefs" / "availability_receipts" / "2026-08-22.jsonl"
    receipt = json.loads(receipt_path.read_text().splitlines()[0])
    receipt["payload_sha256"] = payload_sha256(row)
    receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    reader = WorldContextReader(news_store=news, company_dir=tmp_path / "company", clock=_clock_at(BOOT))
    evidence = reader.lookup_macro(venue="XTAI", cutoff_at=CUTOFF)
    assert evidence.status == "missing"
    assert evidence.reason == NO_PROVEN_ARTIFACT_REASON
    assert evidence.proven is False


def test_missing_late_and_unproven_share_canonical_evidence(tmp_path: Path) -> None:
    empty_reader = WorldContextReader(
        news_dir=tmp_path / "empty_news",
        company_dir=tmp_path / "empty_company",
        clock=_clock_at(BOOT),
    )
    missing = empty_reader.lookup_macro(venue="XTAI", cutoff_at=CUTOFF)

    late_store = NewsMacroBriefStore(
        tmp_path / "late_news",
        clock=lambda: datetime(2026, 8, 22, 11, 0, tzinfo=timezone.utc),
    )
    late_store.append(_macro())
    late = WorldContextReader(
        news_store=late_store,
        company_dir=tmp_path / "company",
        clock=_clock_at(datetime(2026, 8, 22, 11, 0, tzinfo=timezone.utc)),
    ).lookup_macro(venue="XTAI", cutoff_at=CUTOFF)

    legacy_dir = tmp_path / "legacy_news"
    legacy_dir.mkdir()
    (legacy_dir / "2026-08-22.jsonl").write_text(
        json.dumps(_macro().to_dict(), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    unproven = WorldContextReader(
        news_dir=legacy_dir,
        company_dir=tmp_path / "company",
        clock=_clock_at(BOOT),
    ).lookup_macro(venue="XTAI", cutoff_at=CUTOFF)

    for evidence in (missing, late, unproven):
        assert evidence.status == "missing"
        assert evidence.reason == NO_PROVEN_ARTIFACT_REASON
        assert evidence.proven is False
        assert evidence.payload is None
        assert evidence.artifact is None


def test_receipt_declared_before_cutoff_but_first_seen_after_stays_missing(tmp_path: Path) -> None:
    clock = {"now": datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc)}
    news = NewsMacroBriefStore(
        tmp_path / "news_briefs",
        clock=lambda: datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc),
    )
    reader = WorldContextReader(
        news_store=news,
        company_dir=tmp_path / "company",
        clock=lambda: clock["now"],
    )
    before = reader.lookup_macro(venue="XTAI", cutoff_at=CUTOFF)
    assert before.status == "missing"
    assert before.reason == NO_PROVEN_ARTIFACT_REASON

    news.append(_macro())
    clock["now"] = datetime(2026, 8, 22, 10, 30, tzinfo=timezone.utc)
    after = reader.lookup_macro(venue="XTAI", cutoff_at=CUTOFF)
    assert after.status == "missing"
    assert after.reason == NO_PROVEN_ARTIFACT_REASON
    assert after.proven is False
    assert after.artifact is None
    assert after.payload is None


def test_receipt_primed_before_later_cutoff_becomes_eligible(tmp_path: Path) -> None:
    ready = datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc)
    news = NewsMacroBriefStore(tmp_path / "news_briefs", clock=lambda: ready)
    news.append(_macro())
    boot = datetime(2026, 8, 22, 9, 30, tzinfo=timezone.utc)
    reader = WorldContextReader(
        news_store=news,
        company_dir=tmp_path / "company",
        clock=_clock_at(boot),
    )
    evidence = reader.lookup_macro(venue="XTAI", cutoff_at=CUTOFF)
    assert evidence.status == "complete"
    assert evidence.proven is True
    assert evidence.artifact is not None
    assert evidence.artifact.ready_at == boot


def test_boot_scan_does_not_backdate_receipt_observed_after_cutoff(tmp_path: Path, monkeypatch) -> None:
    news = NewsMacroBriefStore(
        tmp_path / "news_briefs",
        clock=lambda: datetime(2026, 8, 22, 9, 0, tzinfo=timezone.utc),
    )
    clock = {"now": datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)}
    injected = {"done": False}
    original_glob = Path.glob

    def glob_hook(self: Path, pattern: str, *args: object, **kwargs: object):
        path = str(self)
        if not injected["done"] and pattern == "*.jsonl" and "availability_receipts" in path and "news_briefs" in path:
            injected["done"] = True
            news.append(_macro())
            clock["now"] = datetime(2026, 8, 22, 10, 30, tzinfo=timezone.utc)
        return original_glob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", glob_hook)
    reader = WorldContextReader(
        news_store=news,
        company_dir=tmp_path / "company",
        clock=lambda: clock["now"],
    )
    evidence = reader.lookup_macro(venue="XTAI", cutoff_at=CUTOFF)
    assert evidence.status == "missing"
    assert evidence.reason == NO_PROVEN_ARTIFACT_REASON
    assert evidence.proven is False
    assert evidence.artifact is None
    assert evidence.payload is None
    later = reader.lookup_macro(venue="XTAI", cutoff_at=datetime(2026, 8, 22, 11, 0, tzinfo=timezone.utc))
    assert later.status == "complete"
    assert later.proven is True
    assert later.artifact is not None
    assert later.artifact.ready_at == datetime(2026, 8, 22, 10, 30, tzinfo=timezone.utc)
