"""Thin CLI adapter for shadow world-model status and cohort commands.

Parse/dispatch only: no SQLite, no status strings written by the adapter, and
no implicit arm/start. Reads go through the reporting/query projectors. Mutations
go through typed application commands.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trader.application.world_model.cohort_service import WorldCohortService
from trader.domain.world_cohort import (
    COHORT_AUTHORITY,
    COHORT_DECISION_EFFECT,
    COHORT_RECOMMENDATION,
    COHORT_TRADER_CONTRIBUTION,
    ArmWorldCohort,
    CloseWorldCohort,
    InvalidateWorldCohort,
    InvalidationReason,
    RegisterWorldCohort,
    SensorMask,
    StartWorldCohort,
    WorldCohort,
    WorldCohortArmed,
    WorldCohortEventEnvelope,
    WorldCohortId,
    WorldCohortManifest,
)
from trader.reporting.read_models.world_cohort import read_world_cohort_report
from trader.reporting.read_models.world_graph import read_world_graph_report, read_world_graph_status
from trader.reporting.read_models.world_macro_status import read_world_macro_status
from trader.reporting.read_models.world_patterns import read_world_pattern_report, read_world_pattern_status
from trader.reporting.read_models.world_status import HORIZONS, read_world_model_status

WORLD_COHORT_INVALIDATION_REASONS = tuple(item.value for item in InvalidationReason)
_WORLD_MODEL_DB = "world_model.db"
_CLAIM_FIELDS = {
    "authority": COHORT_AUTHORITY,
    "decision_effect": COHORT_DECISION_EFFECT,
    "recommendation": COHORT_RECOMMENDATION,
    "causal_claim": False,
    "pnl_claim": False,
    "actual_trader_contribution": COHORT_TRADER_CONTRIBUTION,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _db_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / _WORLD_MODEL_DB


def _claims() -> dict[str, Any]:
    return dict(_CLAIM_FIELDS)


def _error(
    code: str,
    message: str,
    *,
    context: Mapping[str, Any] | None = None,
    recovery: str | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if context is not None:
        error["context"] = dict(context)
    if recovery is not None:
        error["recovery"] = recovery
    return {"ok": False, "error": error, **_claims()}


def _load_manifest(manifest_path: str | Path) -> WorldCohortManifest:
    payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise TypeError("manifest must be a JSON object")
    return WorldCohortManifest.from_mapping(payload)


def validate_world_cohort_manifest(manifest_path: str | Path) -> tuple[dict[str, Any], int]:
    try:
        manifest = _load_manifest(manifest_path)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return (
            _error(
                "invalid_manifest",
                f"{type(exc).__name__}:{exc}",
                context={"manifest_path": str(manifest_path)},
                recovery="fix the manifest JSON and retry validate",
            ),
            1,
        )
    return (
        {
            "ok": True,
            "command": "validate",
            "schema_version": manifest.schema_version,
            "cohort_id": manifest.cohort_id,
            "manifest_sha256": manifest.manifest_sha256,
            "study_kind": manifest.study_kind.value,
            **_claims(),
        },
        0,
    )


def read_world_cohort_status(state_dir: str | Path, cohort_id: str) -> dict[str, Any]:
    from trader.infrastructure.state_db.world_model_query import read_world_cohort_ledger

    db_path = _db_path(state_dir)
    ledger = read_world_cohort_ledger(db_path, cohort_id, include_predictions=False)
    payload: dict[str, Any] = {
        "schema_version": "world_cohort_status.v1",
        "command": "status",
        "cohort_id": cohort_id,
        "db_path": str(db_path),
        "exists": bool(ledger.get("exists")),
        "status": ledger.get("status"),
        "phase": None,
        "manifest_sha256": None,
        "study_kind": None,
        "event_types": [],
        "started_event_id": None,
        **_claims(),
    }
    if "missing_tables" in ledger:
        payload["missing_tables"] = list(ledger["missing_tables"])
    if "error" in ledger:
        payload["error"] = ledger["error"]
    if ledger.get("status") != "loaded":
        return payload
    try:
        cohort = WorldCohort.reconstruct(ledger.get("manifest") or {}, ledger.get("events") or ())
    except (TypeError, ValueError) as exc:
        payload["status"] = "unavailable"
        payload["error"] = f"{type(exc).__name__}:{exc}"
        return payload
    payload.update(
        {
            "ok": True,
            "cohort_id": cohort.cohort_id,
            "manifest_sha256": cohort.manifest.manifest_sha256,
            "phase": cohort.phase.value,
            "study_kind": cohort.manifest.study_kind.value,
            "event_types": [event.event_type for event in cohort.events],
            "started_event_id": None if cohort.started_event is None else cohort.started_event.event_id,
        }
    )
    return payload


def _read_exit_code(payload: Mapping[str, Any]) -> int:
    return 1 if payload.get("status") in {"unavailable", "schema_unavailable"} else 0


@contextmanager
def _cohort_service(state_dir: str | Path, *, create: bool):
    from trader.infrastructure.state_db.world_model_store import WorldModelStore

    path = _db_path(state_dir)
    if not create and not path.exists():
        raise FileNotFoundError(str(path))
    store = WorldModelStore(path)
    try:
        yield WorldCohortService(repository=store, query=store)
    finally:
        store.close()


def _command_payload(command: str, envelope: WorldCohortEventEnvelope, cohort: WorldCohort) -> dict[str, Any]:
    event = envelope.event
    payload: dict[str, Any] = {
        "ok": True,
        "command": command,
        "cohort_id": cohort.cohort_id,
        "manifest_sha256": cohort.manifest.manifest_sha256,
        "phase": cohort.phase.value,
        "event_type": event.event_type,
        "event_id": envelope.event_id,
        "sequence": envelope.sequence,
        "availability_status": envelope.availability_status,
        "envelope": envelope.to_dict(),
        **_claims(),
    }
    if isinstance(event, WorldCohortArmed):
        payload["satisfied_sensor_ids"] = list(event.satisfied_sensor_ids)
    return payload


def _mutate(
    state_dir: str | Path,
    *,
    command: str,
    create: bool,
    run: Callable[[WorldCohortService], WorldCohortEventEnvelope],
) -> tuple[dict[str, Any], int]:
    from trader.infrastructure.state_db.world_model_store import WorldModelConflictError

    try:
        with _cohort_service(state_dir, create=create) as service:
            envelope = run(service)
            cohort = service.repository.load(WorldCohortId(envelope.cohort_id))
            return _command_payload(command, envelope, cohort), 0
    except FileNotFoundError as exc:
        return (
            _error(
                "store_missing",
                str(exc),
                recovery="casys-trader world cohort register --manifest PATH",
            ),
            1,
        )
    except LookupError as exc:
        return (
            _error(
                "unknown_cohort",
                str(exc),
                recovery="register the cohort_id before this mutation",
            ),
            1,
        )
    except WorldModelConflictError as exc:
        return (_error("conflict", str(exc)), 1)
    except (TypeError, ValueError) as exc:
        return (_error("domain_error", f"{type(exc).__name__}:{exc}"), 1)


def register_world_cohort(state_dir: str | Path, manifest_path: str | Path) -> tuple[dict[str, Any], int]:
    try:
        manifest = _load_manifest(manifest_path)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return (
            _error(
                "invalid_manifest",
                f"{type(exc).__name__}:{exc}",
                context={"manifest_path": str(manifest_path)},
                recovery="fix the manifest JSON and retry register",
            ),
            1,
        )
    return _mutate(
        state_dir,
        command="register",
        create=True,
        run=lambda service: service.register(RegisterWorldCohort(manifest=manifest)),
    )


def _load_cohort(service: WorldCohortService, cohort_id: str) -> WorldCohort:
    return service.repository.load(WorldCohortId(cohort_id))


def arm_world_cohort(
    state_dir: str | Path,
    cohort_id: str,
    sensors: Sequence[str] | None = None,
) -> tuple[dict[str, Any], int]:
    def run(service: WorldCohortService) -> WorldCohortEventEnvelope:
        cohort = _load_cohort(service, cohort_id)
        satisfied = (
            tuple(sensors)
            if sensors is not None
            else tuple(
                item.sensor_id
                for item in cohort.manifest.sensor_requirements
                if item.mode is SensorMask.REQUIRED
            )
        )
        return service.arm(
            ArmWorldCohort(
                cohort_id=cohort.cohort_id,
                manifest_sha256=cohort.manifest.manifest_sha256,
                runtime_identity=cohort.manifest.runtime_identity,
                satisfied_sensor_ids=satisfied,
            )
        )

    return _mutate(state_dir, command="arm", create=False, run=run)


def start_world_cohort(state_dir: str | Path, cohort_id: str) -> tuple[dict[str, Any], int]:
    def run(service: WorldCohortService) -> WorldCohortEventEnvelope:
        cohort = _load_cohort(service, cohort_id)
        return service.start(
            StartWorldCohort(
                cohort_id=cohort.cohort_id,
                manifest_sha256=cohort.manifest.manifest_sha256,
                runtime_identity=cohort.manifest.runtime_identity,
            )
        )

    return _mutate(state_dir, command="start", create=False, run=run)


def close_world_cohort(state_dir: str | Path, cohort_id: str, reason: str) -> tuple[dict[str, Any], int]:
    return _mutate(
        state_dir,
        command="close",
        create=False,
        run=lambda service: service.close(cohort_id, CloseWorldCohort(reason=reason)),
    )


def invalidate_world_cohort(
    state_dir: str | Path,
    cohort_id: str,
    *,
    reason: str,
    scope: str | None = None,
    proofs: Sequence[str] | None = None,
    occurred_at: str | datetime | None = None,
) -> tuple[dict[str, Any], int]:
    return _mutate(
        state_dir,
        command="invalidate",
        create=False,
        run=lambda service: service.invalidate(
            cohort_id,
            InvalidateWorldCohort(
                reason=reason,
                scope=scope or cohort_id,
                proofs=tuple(proofs) if proofs else ("operator_cli",),
                occurred_at=occurred_at or _utc_now(),
            ),
        ),
    )


def dispatch_world_cohort(args: Any, *, state_dir: str | Path) -> tuple[dict[str, Any], int]:
    command = getattr(args, "cohort_command", None)
    if command == "validate":
        return validate_world_cohort_manifest(args.manifest)
    if command == "register":
        return register_world_cohort(state_dir, args.manifest)
    if command == "arm":
        return arm_world_cohort(state_dir, args.cohort_id, sensors=args.sensor)
    if command == "start":
        return start_world_cohort(state_dir, args.cohort_id)
    if command == "status":
        payload = read_world_cohort_status(state_dir, args.cohort_id)
        return payload, _read_exit_code(payload)
    if command == "report":
        payload = read_world_cohort_report(state_dir, args.cohort_id)
        return payload, _read_exit_code(payload)
    if command == "close":
        return close_world_cohort(state_dir, args.cohort_id, args.reason)
    if command == "invalidate":
        return invalidate_world_cohort(
            state_dir,
            args.cohort_id,
            reason=args.reason,
            scope=args.scope,
            proofs=args.proof,
            occurred_at=args.occurred_at,
        )
    return _error("unsupported_command", str(command), recovery="use a documented world cohort subcommand"), 2


def read_world_macro_cli_status(state_dir: str | Path) -> dict[str, Any]:
    payload = read_world_macro_status(state_dir)
    return {"command": "status", **payload}


def dispatch_world_macro(args: Any, *, state_dir: str | Path) -> tuple[dict[str, Any], int]:
    command = getattr(args, "macro_command", None)
    if command == "status":
        payload = read_world_macro_cli_status(state_dir)
        return payload, _read_exit_code(payload)
    return _error("unsupported_command", str(command), recovery="use a documented world macro subcommand"), 2


def dispatch_world_graph(args: Any, *, state_dir: str | Path) -> tuple[dict[str, Any], int]:
    command = getattr(args, "graph_command", None)
    if command == "status":
        payload = read_world_graph_status(state_dir)
        return payload, _read_exit_code(payload)
    if command == "report":
        payload = read_world_graph_report(state_dir, getattr(args, "cohort_id", None))
        return payload, _read_exit_code(payload)
    return _error("unsupported_command", str(command), recovery="use a documented world graph subcommand"), 2


def reconcile_world_scope_mapping(
    config_dir: str | Path,
    *,
    persist: bool = False,
) -> tuple[dict[str, Any], int]:
    from trader.application.world_model.scope_mapping_reconcile import WorldScopeMappingReconcileService
    from trader.infrastructure.files.universe_anchors import YamlUniverseAnchorSource
    from trader.infrastructure.files.world_scope_mapping_config import YamlWorldScopeMappingStore
    from trader.infrastructure.market_sources.world_scope_listing import (
        InstrumentListingMetadataAdapter,
        YFinanceListingExchangeLookup,
    )

    service = WorldScopeMappingReconcileService(
        universe=YamlUniverseAnchorSource(config_dir),
        listings=InstrumentListingMetadataAdapter(exchange_lookup=YFinanceListingExchangeLookup()),
        store=YamlWorldScopeMappingStore(config_dir),
    )
    result = service.reconcile(persist=persist)
    return {
        "ok": True,
        "command": "scope-reconcile",
        "persist": persist,
        "action": result.action,
        "mapping_id": result.mapping.mapping_id,
        "mapping_sha256": result.mapping.content_sha256,
        "added_anchors": [list(item) for item in result.added_anchors],
        "preserved_conflicts": [list(item) for item in result.preserved_conflicts],
        "unresolved": [
            {
                "market_venue": item.market_venue,
                "instrument": item.instrument,
                "status": item.status,
                "reason": item.reason,
            }
            for item in result.unresolved
        ],
        **_claims(),
    }, 0


def dispatch_world_scope(args: Any, *, config_dir: str | Path) -> tuple[dict[str, Any], int]:
    command = getattr(args, "scope_command", None)
    if command == "reconcile":
        return reconcile_world_scope_mapping(config_dir, persist=bool(getattr(args, "apply", False)))
    return _error("unsupported_command", str(command), recovery="casys-trader world scope reconcile"), 2


def _required_apply_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is required")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _sha256_hex_digest(value: Any, field_name: str) -> str:
    text = _required_apply_text(value, field_name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{field_name} must be a sha256 hex digest")
    return text


def _selected_pattern_candidates(result: Any, hypothesis_ids: Sequence[str] | None) -> tuple[Any, ...]:
    displayed = tuple(result.candidates)
    by_id = {item.spec.hypothesis_id: item for item in displayed}
    requested = tuple(str(item).strip() for item in (hypothesis_ids or ()) if str(item).strip())
    if not requested:
        return displayed
    if len(requested) != len(set(requested)):
        raise ValueError("hypothesis_id selections must be unique")
    missing = [item for item in requested if item not in by_id]
    if missing:
        raise ValueError(f"unknown hypothesis_id: {missing[0]}")
    return tuple(by_id[item] for item in requested)


def _source_evidence_fields(result: Any, *, include_source_evidence: bool) -> dict[str, Any]:
    from trader.domain.world_episode import canonical_sha256

    evidence_ids = list(result.source_evidence_ids)
    fields: dict[str, Any] = {
        "source_evidence_count": len(evidence_ids),
        "source_evidence_fingerprint": canonical_sha256(sorted(evidence_ids)),
    }
    if include_source_evidence:
        fields["source_evidence_ids"] = evidence_ids
    return fields


def _discover_payload(
    state_dir: str | Path,
    request: Any,
    result: Any,
    *,
    include_source_evidence: bool = False,
) -> dict[str, Any]:
    db_path = _db_path(state_dir)
    body = result.to_dict()
    body.pop("source_evidence_ids", None)
    body.update(_source_evidence_fields(result, include_source_evidence=include_source_evidence))
    return {
        "ok": True,
        "command": "discover",
        "apply": False,
        "db_path": str(db_path),
        "exists": db_path.exists(),
        "request": request.to_dict(),
        **body,
        **_claims(),
    }


def discover_world_patterns(
    state_dir: str | Path,
    *,
    formation_cutoff: str | datetime,
    evaluation_start_not_before: str | datetime,
    ontology_revision: str | None = None,
    horizons: Sequence[str] | None = None,
    min_support: int = 20,
    min_association: float = 0.10,
    max_candidates: int = 20,
    smoothing_alpha: float = 1.0,
    apply: bool = False,
    evaluation_cohort_id: str | None = None,
    evaluation_dataset_fingerprint: str | None = None,
    hypothesis_ids: Sequence[str] | None = None,
    include_source_evidence: bool = False,
) -> tuple[dict[str, Any], int]:
    from trader.application.world_model.pattern_discovery import PatternDiscoveryService
    from trader.application.world_model.pattern_formation_request import PatternFormationRequest
    from trader.infrastructure.state_db.world_pattern_formation_query import SqlitePatternFormationSource

    persist = bool(apply)
    cohort_id = None
    eval_fingerprint = None
    if persist:
        try:
            cohort_id = WorldCohortId(
                _required_apply_text(evaluation_cohort_id, "evaluation_cohort_id")
            ).value
            eval_fingerprint = _sha256_hex_digest(
                evaluation_dataset_fingerprint,
                "evaluation_dataset_fingerprint",
            )
        except (TypeError, ValueError) as exc:
            return (_error("invalid_apply", f"{type(exc).__name__}:{exc}"), 1)
    try:
        request_kwargs: dict[str, Any] = {
            "formation_cutoff": formation_cutoff,
            "evaluation_start_not_before": evaluation_start_not_before,
            "ontology_revision": ontology_revision,
            "min_support": min_support,
            "min_association": min_association,
            "max_candidates": max_candidates,
            "smoothing_alpha": smoothing_alpha,
        }
        if horizons is not None:
            request_kwargs["horizons"] = horizons
        request = PatternFormationRequest(**request_kwargs)
    except (TypeError, ValueError) as exc:
        return (_error("domain_error", f"{type(exc).__name__}:{exc}"), 1)

    result = PatternDiscoveryService(
        SqlitePatternFormationSource(_db_path(state_dir), macro_root=Path(state_dir) / "world_macro")
    ).discover(request)
    payload = _discover_payload(
        state_dir,
        request,
        result,
        include_source_evidence=bool(include_source_evidence),
    )
    if not persist:
        return payload, 0

    try:
        selected = _selected_pattern_candidates(result, hypothesis_ids)
    except ValueError as exc:
        code = "unknown_hypothesis" if str(exc).startswith("unknown hypothesis_id") else "domain_error"
        return (_error(code, str(exc), context={"apply": True}), 1)

    forbidden = {result.formation_dataset_fingerprint}
    forbidden.update(item.spec.formation_dataset_fingerprint for item in selected)
    if eval_fingerprint in forbidden:
        return (
            _error(
                "in_sample_dataset",
                "in-sample confirmation is forbidden: evaluation dataset must differ from formation",
                context={
                    "apply": True,
                    "evaluation_dataset_fingerprint": eval_fingerprint,
                    "formation_dataset_fingerprint": result.formation_dataset_fingerprint,
                },
            ),
            1,
        )

    db_path = _db_path(state_dir)
    if not db_path.exists():
        return (
            _error(
                "store_missing",
                str(db_path),
                recovery="create world_model.db before --apply; dry-run does not create it",
            ),
            1,
        )

    from trader.application.world_model.pattern_ports import PatternPayloadConflict
    from trader.application.world_model.pattern_service import (
        RegisterPatternHypothesis,
        StartPatternEvaluation,
        WorldPatternService,
    )
    from trader.domain.world_pattern import PatternHypothesisId
    from trader.infrastructure.state_db.world_pattern_store import WorldPatternStore

    store = WorldPatternStore(db_path)
    try:
        service = WorldPatternService(hypotheses=store, occurrences=store, availability=store)
        applied: list[dict[str, Any]] = []
        for candidate in selected:
            spec = candidate.spec
            registered = service.register(
                RegisterPatternHypothesis(spec=spec, registered_at=spec.formation_cutoff)
            )
            started = service.start(
                StartPatternEvaluation(
                    hypothesis_id=registered.event.hypothesis_id,
                    evaluation_cohort_id=cohort_id,
                    started_at=spec.evaluation_start_not_before,
                    evaluation_dataset_fingerprint=eval_fingerprint,
                )
            )
            hypothesis = store.load(PatternHypothesisId(registered.event.hypothesis_id))
            applied.append(
                {
                    "hypothesis_id": registered.event.hypothesis_id,
                    "registered_event_id": registered.event.event_id,
                    "started_event_id": started.event.event_id,
                    "status": hypothesis.status,
                    "registered_at": spec.formation_cutoff.isoformat(),
                    "started_at": spec.evaluation_start_not_before.isoformat(),
                }
            )
    except PatternPayloadConflict as exc:
        return (_error("conflict", str(exc), context={"apply": True}), 1)
    except (LookupError, TypeError, ValueError) as exc:
        return (_error("domain_error", f"{type(exc).__name__}:{exc}", context={"apply": True}), 1)
    finally:
        store.close()

    payload.update(
        {
            "apply": True,
            "exists": True,
            "evaluation_cohort_id": cohort_id,
            "evaluation_dataset_fingerprint": eval_fingerprint,
            "selected_hypothesis_ids": [item["hypothesis_id"] for item in applied],
            "applied": applied,
        }
    )
    return payload, 0


def read_world_pattern_cli_report(state_dir: str | Path, cohort_id: str) -> dict[str, Any]:
    return {"command": "report", **read_world_pattern_report(state_dir, cohort_id)}


def read_world_pattern_cli_status(state_dir: str | Path, cohort_id: str | None = None) -> dict[str, Any]:
    return {"command": "status", **read_world_pattern_status(state_dir, cohort_id)}


def evaluate_world_patterns(
    state_dir: str | Path,
    *,
    as_of: str | datetime,
    evaluation_cohort_id: str,
    evaluation_dataset_fingerprint: str,
    hypothesis_ids: Sequence[str] | None = None,
    apply: bool = False,
) -> tuple[dict[str, Any], int]:
    """Prospective unlabeled matching bound to one evaluation cohort's graph slots."""
    from trader.application.world_model.pattern_evaluation import PatternEvaluationService
    from trader.application.world_model.pattern_evaluation_request import PatternEvaluationRequest
    from trader.infrastructure.state_db.world_pattern_catalog_query import SqlitePatternCatalogQuery
    from trader.infrastructure.state_db.world_pattern_evaluation_query import SqlitePatternEvaluationSource

    persist = bool(apply)
    try:
        request = PatternEvaluationRequest(
            as_of=as_of,
            evaluation_cohort_id=evaluation_cohort_id,
            evaluation_dataset_fingerprint=evaluation_dataset_fingerprint,
            hypothesis_ids=hypothesis_ids,
        )
    except (TypeError, ValueError) as exc:
        return (_error("domain_error", f"{type(exc).__name__}:{exc}"), 1)
    db_path = _db_path(state_dir)
    result = PatternEvaluationService(
        catalog=SqlitePatternCatalogQuery(db_path),
        source=SqlitePatternEvaluationSource(db_path, macro_root=Path(state_dir) / "world_macro"),
    ).evaluate(request)
    payload: dict[str, Any] = {
        "ok": True,
        "command": "evaluate",
        "apply": False,
        "db_path": str(db_path),
        "exists": db_path.exists(),
        **result.to_dict(),
        **_claims(),
    }
    if not persist:
        return payload, 0
    if not db_path.exists():
        return (
            _error(
                "store_missing",
                str(db_path),
                recovery="create world_model.db before --apply; dry-run does not create it",
            ),
            1,
        )
    from trader.application.world_model.pattern_ports import PatternPayloadConflict
    from trader.application.world_model.pattern_service import WorldPatternService
    from trader.infrastructure.state_db.world_model_store import WorldModelConflictError, WorldModelStore
    from trader.infrastructure.state_db.world_pattern_store import WorldPatternStore

    world = WorldModelStore(db_path)
    try:
        store = WorldPatternStore(world._db)
        persisted = PatternEvaluationService(
            catalog=store,
            source=SqlitePatternEvaluationSource(db_path, macro_root=Path(state_dir) / "world_macro"),
        ).persist(
            result,
            patterns=WorldPatternService(hypotheses=store, occurrences=store, availability=store),
            predictions=world,
        )
    except PatternPayloadConflict as exc:
        return (_error("conflict", str(exc), context={"apply": True}), 1)
    except WorldModelConflictError as exc:
        return (_error("conflict", str(exc), context={"apply": True}), 1)
    except (LookupError, TypeError, ValueError) as exc:
        return (_error("domain_error", f"{type(exc).__name__}:{exc}", context={"apply": True}), 1)
    finally:
        world.close()
    payload.update(persisted.to_dict())
    payload["exists"] = True
    payload["apply"] = True
    return payload, 0


def link_world_pattern_outcomes(
    state_dir: str | Path,
    *,
    as_of: str | datetime,
    evaluation_cohort_id: str | None = None,
    hypothesis_ids: Sequence[str] | None = None,
    occurrence_ids: Sequence[str] | None = None,
    apply: bool = False,
) -> tuple[dict[str, Any], int]:
    from trader.application.world_model.pattern_outcome_link import (
        PatternOutcomeLinkRequest,
        PatternOutcomeLinkService,
    )
    from trader.infrastructure.state_db.world_pattern_catalog_query import SqlitePatternCatalogQuery
    from trader.infrastructure.state_db.world_pattern_outcome_query import SqlitePatternOutcomeLeafQuery

    persist = bool(apply)
    try:
        request = PatternOutcomeLinkRequest(
            as_of=as_of,
            evaluation_cohort_id=evaluation_cohort_id,
            hypothesis_ids=hypothesis_ids,
            occurrence_ids=occurrence_ids,
        )
    except (TypeError, ValueError) as exc:
        return (_error("domain_error", f"{type(exc).__name__}:{exc}"), 1)
    db_path = _db_path(state_dir)
    service = PatternOutcomeLinkService(
        catalog=SqlitePatternCatalogQuery(db_path),
        outcomes=SqlitePatternOutcomeLeafQuery(db_path),
    )
    if not persist:
        result = service.preview(request)
        return (
            {
                "ok": True,
                "command": "link-outcomes",
                "apply": False,
                "db_path": str(db_path),
                "exists": db_path.exists(),
                **result.to_dict(),
                **_claims(),
            },
            0,
        )
    if not db_path.exists():
        return (
            _error(
                "store_missing",
                str(db_path),
                recovery="create world_model.db before --apply; dry-run does not create it",
            ),
            1,
        )
    from trader.application.world_model.pattern_ports import PatternPayloadConflict
    from trader.application.world_model.pattern_service import WorldPatternService
    from trader.infrastructure.state_db.world_model_store import WorldModelConflictError, WorldModelStore
    from trader.infrastructure.state_db.world_pattern_store import WorldPatternStore

    world = WorldModelStore(db_path)
    try:
        store = WorldPatternStore(world._db)
        result = PatternOutcomeLinkService(
            catalog=store,
            outcomes=SqlitePatternOutcomeLeafQuery(db_path),
        ).link(
            request,
            patterns=WorldPatternService(hypotheses=store, occurrences=store, availability=store),
        )
    except PatternPayloadConflict as exc:
        return (_error("conflict", str(exc), context={"apply": True}), 1)
    except WorldModelConflictError as exc:
        return (_error("conflict", str(exc), context={"apply": True}), 1)
    except (LookupError, TypeError, ValueError) as exc:
        return (_error("domain_error", f"{type(exc).__name__}:{exc}", context={"apply": True}), 1)
    finally:
        world.close()
    return (
        {
            "ok": True,
            "command": "link-outcomes",
            "apply": True,
            "db_path": str(db_path),
            "exists": True,
            **result.to_dict(),
            **_claims(),
        },
        0,
    )


def dispatch_world_pattern(args: Any, *, state_dir: str | Path) -> tuple[dict[str, Any], int]:
    command = getattr(args, "pattern_command", None)
    if command == "discover":
        horizons = getattr(args, "horizon", None)
        return discover_world_patterns(
            state_dir,
            formation_cutoff=args.formation_cutoff,
            evaluation_start_not_before=args.evaluation_start_not_before,
            ontology_revision=getattr(args, "ontology_revision", None),
            horizons=None if not horizons else tuple(horizons),
            min_support=args.min_support,
            min_association=args.min_association,
            max_candidates=args.max_candidates,
            smoothing_alpha=args.smoothing_alpha,
            apply=bool(getattr(args, "apply", False)),
            evaluation_cohort_id=getattr(args, "evaluation_cohort_id", None),
            evaluation_dataset_fingerprint=getattr(args, "evaluation_dataset_fingerprint", None),
            hypothesis_ids=getattr(args, "hypothesis_id", None),
            include_source_evidence=bool(getattr(args, "include_source_evidence", False)),
        )
    if command == "evaluate":
        return evaluate_world_patterns(
            state_dir,
            as_of=args.as_of,
            evaluation_cohort_id=args.evaluation_cohort_id,
            evaluation_dataset_fingerprint=args.evaluation_dataset_fingerprint,
            hypothesis_ids=getattr(args, "hypothesis_id", None),
            apply=bool(getattr(args, "apply", False)),
        )
    if command == "link-outcomes":
        return link_world_pattern_outcomes(
            state_dir,
            as_of=args.as_of,
            evaluation_cohort_id=getattr(args, "evaluation_cohort_id", None),
            hypothesis_ids=getattr(args, "hypothesis_id", None),
            occurrence_ids=getattr(args, "occurrence_id", None),
            apply=bool(getattr(args, "apply", False)),
        )
    if command == "status":
        payload = read_world_pattern_cli_status(state_dir, getattr(args, "cohort_id", None))
        return payload, _read_exit_code(payload)
    if command == "report":
        payload = read_world_pattern_cli_report(state_dir, args.cohort_id)
        return payload, _read_exit_code(payload)
    return (
        _error(
            "unsupported_command",
            str(command),
            recovery="use world pattern discover, evaluate, link-outcomes, status, or report",
        ),
        2,
    )


__all__ = [
    "HORIZONS",
    "WORLD_COHORT_INVALIDATION_REASONS",
    "arm_world_cohort",
    "close_world_cohort",
    "discover_world_patterns",
    "evaluate_world_patterns",
    "link_world_pattern_outcomes",
    "dispatch_world_cohort",
    "dispatch_world_graph",
    "dispatch_world_macro",
    "dispatch_world_pattern",
    "dispatch_world_scope",
    "reconcile_world_scope_mapping",
    "invalidate_world_cohort",
    "read_world_cohort_report",
    "read_world_cohort_status",
    "read_world_graph_report",
    "read_world_graph_status",
    "read_world_macro_status",
    "read_world_model_status",
    "read_world_pattern_cli_report",
    "read_world_pattern_cli_status",
    "register_world_cohort",
    "start_world_cohort",
    "validate_world_cohort_manifest",
]
