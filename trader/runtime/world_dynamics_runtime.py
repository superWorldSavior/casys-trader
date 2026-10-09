"""Compose automatic OHLCV dynamics on the existing shadow worker."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
import logging
import os
from pathlib import Path

from trader.application.world_model.dynamics_workflow import (
    DynamicsCycleResult,
    DynamicsWorkflowConfig,
    WorldDynamicsShadowWorkflow,
)
from trader.application.world_model.service import _coerce_world_episode
from trader.infrastructure.files.world_dynamics import FilesystemDynamicsArtifacts, FilesystemDynamicsJournal
from trader.reporting.read_models.world_dynamics import project_world_dynamics_result


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _series_key(scope: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(scope[field] for field in (
        "venue", "symbol", "bar_interval", "market_contract_version",
        "sampling_policy_version", "source", "timestamp_semantics",
    ))


class _DynamicsPublisher:
    def __init__(self, artifacts: FilesystemDynamicsArtifacts, *, clock: Callable[[], datetime]) -> None:
        self.artifacts = artifacts
        self.clock = clock
        self.started_at = clock()
        self._latest: dict[str, object] = {}

    def _write(self, summary: Mapping[str, object]) -> None:
        payload = {
            **summary, "pid": os.getpid(), "started_at": self.started_at.isoformat(),
            "updated_at": max(self.started_at, self.clock()).isoformat(),
            "report_command": "casys-trader world dynamics status --json",
        }
        self.artifacts.write_status(payload)
        self._latest = payload

    def state(self, *, enabled: bool, status: str, reason: str, error: Exception | None = None) -> None:
        payload: dict[str, object] = {
            **self._latest,
            "schema_version": "world_dynamics_status.v1", "enabled": enabled,
            "status": status, "reason": reason,
            "authority": "shadow_only", "decision_effect": "none", "recommendation": "NO_GO",
            "causal_claim": False, "pnl_claim": False,
            "series": self._latest.get("series", []),
        }
        if error is not None:
            payload["error"] = f"{type(error).__name__}:{error}"
        self._write(payload)

    def publish(self, publication: DynamicsCycleResult) -> None:
        report_paths: dict[tuple[str, ...], str] = {}
        for run in publication.reports:
            key = _series_key(run.scope)
            report_paths[key] = self.artifacts.write_report(
                key, project_world_dynamics_result(run.result, run.request, run.scope),
            )
        series = []
        for row in publication.summary.get("series", []):
            series.append({**row, "report_path": report_paths.get(_series_key(row))})
        self._write({
            **publication.summary, "series": series,
            "report_paths": list(report_paths.values()),
        })


class WorldDynamicsRuntime:
    """Isolate maintenance failures and persist their operational reason."""

    def __init__(self, workflow: WorldDynamicsShadowWorkflow, publisher: _DynamicsPublisher) -> None:
        self.workflow = workflow
        self.publisher = publisher

    def run(
        self, *, episodes: Iterable[object], bars_by_symbol: Mapping[str, object], now: datetime,
    ) -> DynamicsCycleResult:
        try:
            # The runner freezes MappingProxy-backed domain records through
            # their canonical payload. Reuse its established reconstruction.
            return self.workflow.run(
                episodes=tuple(_coerce_world_episode(episode) for episode in episodes),
                bars_by_symbol=bars_by_symbol, now=now,
            )
        except Exception as exc:  # noqa: BLE001 - shadow error is reported and isolated by the runner
            self.publisher.state(enabled=True, status="unavailable", reason="maintenance_error", error=exc)
            raise

    def skip(self, *, reason: str) -> None:
        self.publisher.state(enabled=True, status="paused", reason=reason)


def compose_world_dynamics_runtime(
    *, state_dir: str | Path, enabled: bool = True, logger: object | None = None,
    clock: Callable[[], datetime] = _now,
) -> WorldDynamicsRuntime | None:
    """Enabled by default inside World Model shadow; no provider or trading port."""
    log = logger or logging.getLogger("casys-trader")
    publisher = _DynamicsPublisher(FilesystemDynamicsArtifacts(state_dir), clock=clock)
    try:
        if not enabled:
            publisher.state(enabled=False, status="disabled", reason="explicit_environment_override")
            return None
        workflow = WorldDynamicsShadowWorkflow(
            journal=FilesystemDynamicsJournal(state_dir, clock=clock), publisher=publisher,
            config=DynamicsWorkflowConfig(), clock=clock,
        )
        publisher.state(enabled=True, status="waiting_for_data", reason="waiting_market_snapshot")
        return WorldDynamicsRuntime(workflow, publisher)
    except Exception as exc:  # noqa: BLE001 - dynamics composition cannot block other shadow lanes
        log.warning("[world_dynamics_shadow] compose failed: %s:%s", type(exc).__name__, exc)
        try:
            publisher.state(enabled=enabled, status="unavailable", reason="composition_error", error=exc)
        except Exception:  # noqa: BLE001 - unwritable status cannot block daemon startup
            pass
        return None
