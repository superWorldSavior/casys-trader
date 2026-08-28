from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from trader.domain.agent_storage_retention import (
    ArchiveSource,
    ArchiveUnit,
    ObservedQuarantineUnit,
    PENDING_RECONCILIATION_SCHEMA,
    PendingReconciliationEntry,
    PendingReconciliationJournal,
    PurgeSource,
    QUARANTINE_INTENT_SCHEMA,
    QuarantineDeletionAuthority,
    QuarantineRecoveryKind,
    QuarantineTransactionIntent,
    QuarantineUnitIntent,
    RetentionProvider,
    SESSION_DOCS_VACUUM_JOURNAL_SCHEMA,
    SessionDocsVacuumJournal,
    acpx_stream_lock_is_recoverable,
    decide_transaction_recovery,
    is_lexically_confined,
    planned_quarantine_path,
    session_docs_vacuum_journal_path,
)


def test_archive_unit_rejects_an_escaping_archive_path() -> None:
    with pytest.raises(ValueError, match="safe relative path"):
        ArchiveUnit(
            path=Path("session"),
            arcname="../outside",
            recorded_at=datetime.now(timezone.utc),
            size=1,
        )


def test_report_only_source_requires_an_explicit_protection_reason() -> None:
    with pytest.raises(ValueError, match="protection_reason"):
        ArchiveSource(
            "codex-home",
            Path("sessions"),
            "sessions",
            "recursive_files",
            apply_enabled=False,
        )


def test_provider_session_layout_cannot_be_applied_to_logs() -> None:
    with pytest.raises(ValueError, match="requires the sessions category"):
        ArchiveSource("grok-home", Path("logs"), "logs", "grok_sessions")


def test_ops_source_cannot_escape_its_authority_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes authority"):
        ArchiveSource(
            "grok-home",
            tmp_path / "outside" / "sessions",
            "sessions",
            "grok_sessions",
            authority_root=tmp_path / "ops",
        )


def test_only_acpx_may_declare_an_external_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ACPX"):
        ArchiveSource(
            "grok-home",
            tmp_path / "sessions",
            "sessions",
            "grok_sessions",
            allows_external_root=True,
        )


def test_acpx_external_root_is_confined_to_its_own_authority(tmp_path: Path) -> None:
    root = tmp_path / "home" / ".acpx" / "sessions"
    source = ArchiveSource(
        "acpx",
        root,
        "sessions",
        "acpx_sessions",
        authority_root=root,
        allows_external_root=True,
    )

    assert source.allows_external_root is True
    assert source.authority_root == root
    assert is_lexically_confined(root / "closed.json", root)


def test_purge_source_cannot_escape_its_authority_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes authority"):
        PurgeSource(
            "codex-home",
            tmp_path / "outside" / "cache",
            authority_root=tmp_path / "ops",
        )


def test_pending_reconciliation_rejects_source_provider_mismatch() -> None:
    with pytest.raises(ValueError, match="provider"):
        PendingReconciliationEntry(
            source_id="grok-home",
            provider=RetentionProvider.ACPX,
            identities=("01a00000-0000-7000-8000-000000000001",),
        )


def test_pending_journal_merges_and_compacts_identities_per_source() -> None:
    first = PendingReconciliationJournal().merge("acpx", {"old-a"})
    merged = first.merge("acpx", {"old-b"}).merge("grok-home", {"01a00000-0000-7000-8000-000000000001"})

    assert merged.schema_version == PENDING_RECONCILIATION_SCHEMA
    assert merged.identities_for("acpx") == frozenset({"old-a", "old-b"})
    compacted = merged.without_identities("acpx", {"old-a", "old-b"})
    assert compacted.identities_for("acpx") == frozenset()
    assert compacted.identities_for("grok-home") == frozenset({"01a00000-0000-7000-8000-000000000001"})
    assert compacted.last_error is None

    failed = compacted.with_error("ACPX index rebuild did not match surviving records")
    assert failed.last_error is not None
    assert failed.identities_for("grok-home") == compacted.identities_for("grok-home")
    round_trip = PendingReconciliationJournal.from_payload(failed.to_payload())
    assert round_trip.identities_for("grok-home") == failed.identities_for("grok-home")
    assert round_trip.last_error == failed.last_error


def test_pending_journal_rejects_unknown_schema_version() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        PendingReconciliationJournal.from_payload({"schema_version": "nope", "entries": []})


_NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)
_FAKE_ARCHIVE_SHA256 = "sha256:" + ("ab" * 32)


def _intent_unit(
    tmp_path: Path,
    name: str,
    identity: str | None = None,
    *,
    identityless: bool = False,
) -> QuarantineUnitIntent:
    guard = tmp_path / "sessions"
    live = guard / name
    return QuarantineUnitIntent(
        live_path=live,
        quarantine_path=planned_quarantine_path(live, guard),
        guard_root=guard,
        fingerprint=f"fp-{name}",
        arcname=f"grok-home/sessions/{name}",
        size=4,
        recorded_at=_NOW,
        identity=None if identityless else (name if identity is None else identity),
    )


def _intent(
    tmp_path: Path,
    *names: str,
    verified: bool = False,
    identityless: bool = False,
) -> QuarantineTransactionIntent:
    units = tuple(_intent_unit(tmp_path, name, identityless=identityless) for name in names)
    published = tmp_path / "archives" / "grok.tar.zst" if verified else None
    return QuarantineTransactionIntent(
        transaction_id="cafebabedeadbeefcafebabedeadbeef",
        source_id=None if identityless else "grok-home",
        created_at=_NOW,
        units=units,
        published_archive=published,
        published_archive_sha256=_FAKE_ARCHIVE_SHA256 if verified else None,
        archive_verified=verified,
    )


def _observe(
    unit: QuarantineUnitIntent,
    *,
    live: bool,
    quarantine: bool,
    matches: bool = True,
) -> ObservedQuarantineUnit:
    return ObservedQuarantineUnit(
        unit=unit,
        live_exists=live,
        quarantine_exists=quarantine,
        quarantine_matches_intent=matches,
    )


def test_planned_quarantine_path_is_inside_the_hidden_quarantine_tree(tmp_path: Path) -> None:
    guard = tmp_path / "sessions"
    live = guard / "%2Frepo" / "01a00000-0000-7000-8000-000000000001"

    quarantined = planned_quarantine_path(live, guard)

    assert quarantined == guard / ".retention-quarantine" / "%2Frepo" / "01a00000-0000-7000-8000-000000000001"
    assert is_lexically_confined(quarantined, guard)


def test_quarantine_unit_intent_rejects_a_path_that_escapes_the_guard(tmp_path: Path) -> None:
    guard = tmp_path / "sessions"
    live = guard / "sess"
    with pytest.raises(ValueError, match="quarantine path"):
        QuarantineUnitIntent(
            live_path=live,
            quarantine_path=tmp_path / "outside",
            guard_root=guard,
            fingerprint="fp",
            arcname="grok-home/sessions/sess",
            size=1,
            recorded_at=_NOW,
            identity="sess",
        )


def test_transaction_intent_requires_source_id_when_units_have_identities(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="source_id"):
        QuarantineTransactionIntent(
            transaction_id="cafebabedeadbeefcafebabedeadbeef",
            units=(_intent_unit(tmp_path, "sess-a"),),
        )


def test_transaction_intent_round_trip_preserves_units_and_verified_archive(tmp_path: Path) -> None:
    original = _intent(tmp_path, "sess-a", "sess-b").with_verified_archive(
        tmp_path / "archives" / "grok.tar.zst",
        _FAKE_ARCHIVE_SHA256,
    )

    restored = QuarantineTransactionIntent.from_payload(original.to_payload())

    assert restored.schema_version == QUARANTINE_INTENT_SCHEMA
    assert restored.transaction_id == original.transaction_id
    assert restored.source_id == "grok-home"
    assert restored.archive_verified is True
    assert restored.published_archive == tmp_path / "archives" / "grok.tar.zst"
    assert restored.published_archive_sha256 == _FAKE_ARCHIVE_SHA256
    assert [unit.identity for unit in restored.units] == ["sess-a", "sess-b"]
    assert restored.units[0].quarantine_path == original.units[0].quarantine_path


def test_verified_intent_requires_published_path_and_canonical_sha256(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="published archive"):
        QuarantineTransactionIntent(
            transaction_id="cafebabedeadbeefcafebabedeadbeef",
            source_id="grok-home",
            created_at=_NOW,
            units=(_intent_unit(tmp_path, "sess-a"),),
            published_archive=tmp_path / "archives" / "grok.tar.zst",
            archive_verified=True,
        )

    with pytest.raises(ValueError, match="sha256"):
        _intent(tmp_path, "sess-a").with_verified_archive(tmp_path / "archives" / "grok.tar.zst", "not-a-digest")


def test_identityless_verified_intent_covers_without_journal(tmp_path: Path) -> None:
    intent = _intent(tmp_path, "old.log", verified=True, identityless=True)
    authority = intent.deletion_authority(PendingReconciliationJournal())

    plan = decide_transaction_recovery(
        intent,
        (_observe(intent.units[0], live=False, quarantine=True),),
        authority,
    )

    assert intent.units[0].identity is None
    assert intent.source_id is None
    assert authority.published_archive_sha256 == _FAKE_ARCHIVE_SHA256
    assert authority.covers(()) is True
    assert plan.kind is QuarantineRecoveryKind.FINALIZE
    assert plan.finalize == intent.units


def test_transaction_intent_rejects_unknown_schema_and_path_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="schema_version"):
        QuarantineTransactionIntent.from_payload({"schema_version": "nope", "transaction_id": "aa", "units": []})

    payload = _intent(tmp_path, "sess-a").to_payload()
    payload["units"][0]["live_path"] = str(tmp_path / "outside" / "sess-a")
    with pytest.raises(ValueError, match="escapes|guard|confined"):
        QuarantineTransactionIntent.from_payload(payload)


def test_recovery_restores_quarantine_when_journal_and_archive_do_not_prove_authority(tmp_path: Path) -> None:
    intent = _intent(tmp_path, "sess-a", "sess-b")
    authority = QuarantineDeletionAuthority.none()

    plan = decide_transaction_recovery(
        intent,
        (
            _observe(intent.units[0], live=False, quarantine=True),
            _observe(intent.units[1], live=True, quarantine=False),
        ),
        authority,
    )

    assert plan.kind is QuarantineRecoveryKind.RESTORE
    assert plan.restore == (intent.units[0],)
    assert plan.finalize == ()


def test_recovery_finalizes_missing_and_leftover_quarantine_only_with_archive_and_journal(tmp_path: Path) -> None:
    intent = _intent(tmp_path, "sess-a", "sess-b", verified=True)
    authority = QuarantineDeletionAuthority(
        archive_verified=True,
        published_archive=intent.published_archive,
        published_archive_sha256=intent.published_archive_sha256,
        journaled_identities=frozenset({"sess-a", "sess-b"}),
    )

    plan = decide_transaction_recovery(
        intent,
        (
            _observe(intent.units[0], live=False, quarantine=True),
            _observe(intent.units[1], live=False, quarantine=False),
        ),
        authority,
    )

    assert plan.kind is QuarantineRecoveryKind.FINALIZE
    assert plan.finalize == intent.units
    assert plan.restore == ()


def test_recovery_fails_closed_when_live_and_quarantine_both_exist(tmp_path: Path) -> None:
    intent = _intent(tmp_path, "sess-a")
    plan = decide_transaction_recovery(
        intent,
        (_observe(intent.units[0], live=True, quarantine=True),),
        QuarantineDeletionAuthority.none(),
    )
    assert plan.kind is QuarantineRecoveryKind.FAIL_CLOSED
    assert "both exist" in plan.reason


def test_recovery_fails_closed_on_live_session_present_in_the_journal(tmp_path: Path) -> None:
    intent = _intent(tmp_path, "sess-a", verified=True)
    authority = QuarantineDeletionAuthority(
        archive_verified=True,
        published_archive=intent.published_archive,
        published_archive_sha256=intent.published_archive_sha256,
        journaled_identities=frozenset({"sess-a"}),
    )

    plan = decide_transaction_recovery(
        intent,
        (_observe(intent.units[0], live=True, quarantine=False),),
        authority,
    )

    assert plan.kind is QuarantineRecoveryKind.FAIL_CLOSED
    assert "live" in plan.reason


def test_recovery_fails_closed_when_journaled_identity_lacks_archive_proof(tmp_path: Path) -> None:
    intent = _intent(tmp_path, "sess-a")
    authority = QuarantineDeletionAuthority(
        archive_verified=False,
        published_archive=None,
        published_archive_sha256=None,
        journaled_identities=frozenset({"sess-a"}),
    )

    plan = decide_transaction_recovery(
        intent,
        (_observe(intent.units[0], live=False, quarantine=True),),
        authority,
    )

    assert plan.kind is QuarantineRecoveryKind.FAIL_CLOSED
    assert "journaled" in plan.reason


def test_recovery_fails_closed_when_source_is_missing_without_deletion_authority(tmp_path: Path) -> None:
    intent = _intent(tmp_path, "sess-a")
    plan = decide_transaction_recovery(
        intent,
        (_observe(intent.units[0], live=False, quarantine=False),),
        QuarantineDeletionAuthority.none(),
    )
    assert plan.kind is QuarantineRecoveryKind.FAIL_CLOSED
    assert "missing" in plan.reason


def test_recovery_fails_closed_when_quarantine_fingerprint_does_not_match_intent(tmp_path: Path) -> None:
    intent = _intent(tmp_path, "sess-a")
    plan = decide_transaction_recovery(
        intent,
        (_observe(intent.units[0], live=False, quarantine=True, matches=False),),
        QuarantineDeletionAuthority.none(),
    )
    assert plan.kind is QuarantineRecoveryKind.FAIL_CLOSED
    assert "fingerprint" in plan.reason


def test_recovery_discards_intent_when_every_unit_is_already_live(tmp_path: Path) -> None:
    intent = _intent(tmp_path, "sess-a")
    plan = decide_transaction_recovery(
        intent,
        (_observe(intent.units[0], live=True, quarantine=False),),
        QuarantineDeletionAuthority.none(),
    )
    assert plan.kind is QuarantineRecoveryKind.DISCARD
    assert plan.restore == ()
    assert plan.finalize == ()


def test_intent_from_archive_unit_reuses_the_lifecycle_object(tmp_path: Path) -> None:
    live = tmp_path / "sessions" / "closed.json"
    unit = ArchiveUnit(
        path=live,
        arcname="acpx/sessions/closed.json",
        recorded_at=_NOW,
        size=8,
        identity="closed",
        fingerprint="fp-closed",
        guard_root=tmp_path / "sessions",
        closure_record=live,
    )

    intent_unit = QuarantineUnitIntent.from_archive_unit(unit)

    assert intent_unit.live_path == live
    assert intent_unit.identity == "closed"
    assert intent_unit.closure_record == live
    assert intent_unit.quarantine_path == planned_quarantine_path(live, unit.guard_root)


def test_acpx_stream_lock_is_recoverable_only_when_recorded_pid_is_proven_dead() -> None:
    assert acpx_stream_lock_is_recoverable(pid=4321, owner_alive=False) is True
    assert acpx_stream_lock_is_recoverable(pid=4321, owner_alive=True) is False
    assert acpx_stream_lock_is_recoverable(pid=None, owner_alive=False) is False
    assert acpx_stream_lock_is_recoverable(pid=4321, owner_alive=None) is False
    assert acpx_stream_lock_is_recoverable(pid=0, owner_alive=False) is False


def test_session_docs_vacuum_journal_round_trips_and_rejects_unknown_schema(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "sessions" / "session_search.sqlite"
    journal = SessionDocsVacuumJournal(
        sqlite_name=sqlite_path.name,
        identities=("old-a", "old-b"),
        created_at=_NOW,
    )
    payload = journal.to_payload()
    assert payload["schema_version"] == SESSION_DOCS_VACUUM_JOURNAL_SCHEMA
    restored = SessionDocsVacuumJournal.from_payload(payload)
    assert restored.identities == ("old-a", "old-b")
    assert restored.sqlite_name == "session_search.sqlite"
    assert session_docs_vacuum_journal_path(sqlite_path) == sqlite_path.with_name(
        "session_search.sqlite.vacuum-journal"
    )
    with pytest.raises(ValueError, match="schema_version"):
        SessionDocsVacuumJournal.from_payload({"schema_version": "nope", "identities": ["old-a"]})
