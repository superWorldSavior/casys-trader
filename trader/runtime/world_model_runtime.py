"""Fail-open background adapter around the World Model application service.

Deterministic capture/predict/mature orchestration lives in
:mod:`trader.application.world_model.service`.  This module keeps the daemon
thread, snapshot freeze, and fail-open wrapping so a shadow worker cannot
raise into the trading cycle.
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
import logging
import threading
from collections.abc import Iterable, Mapping
from datetime import datetime

from trader.application.world_model.service import (
    DEFAULT_HORIZONS,
    WorldModelService,
    _clone,
    _utc,
)


@dataclasses.dataclass(frozen=True)
class _BackgroundSnapshot:
    episodes: tuple[object, ...]
    bars_by_symbol: object
    now: datetime
    reason: str


class WorldModelBackgroundRunner:
    """Coalescing background wrapper around :class:`WorldModelService`.

    ``trigger`` freezes caller-owned observations and bars, then returns as
    soon as a daemon worker is started or a latest snapshot is queued.  The
    worker always matures old episodes before it captures/predicts the new
    snapshot.  It is strictly shadow maintenance: no scheduler, broker,
    RiskGate, Brain, or decision callback is accepted by this class.
    """

    def __init__(
        self,
        *,
        runtime: WorldModelService,
        logger: object | None = None,
        thread_name: str = "world-model-shadow",
    ) -> None:
        self.runtime = runtime
        self.log = logger or logging.getLogger("casys-trader")
        self.thread_name = str(thread_name).strip() or "world-model-shadow"
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._pending: _BackgroundSnapshot | None = None
        self._stopping = False
        self._last_report: dict[str, object] = {}

    def trigger(
        self,
        *,
        episodes: Iterable[object],
        now: datetime,
        bars_by_symbol: Mapping[str, object] | None = None,
        reason: str = "shadow",
    ) -> dict[str, object]:
        """Freeze input and schedule best-effort maintenance without waiting."""

        try:
            snapshot = _BackgroundSnapshot(
                episodes=tuple(_clone(episode) for episode in episodes),
                bars_by_symbol={} if bars_by_symbol is None else _clone(bars_by_symbol),
                now=_utc(now),
                reason=str(reason),
            )
        except Exception as exc:  # noqa: BLE001 - background capture is fail-open
            report = {
                "triggered": False,
                "reason": "snapshot_error",
                "error": f"{type(exc).__name__}:{exc}",
            }
            self._record_failure(report)
            return report

        with self._lock:
            if self._stopping:
                return {"triggered": False, "reason": "stopping"}
            if self._thread is not None:
                self._pending = snapshot
                return {"triggered": False, "reason": "queued_latest"}
            thread = threading.Thread(
                target=self._run_loop,
                args=(snapshot,),
                daemon=True,
                name=self.thread_name,
            )
            self._thread = thread
            try:
                thread.start()
            except Exception as exc:  # noqa: BLE001 - no worker startup can affect trading
                self._thread = None
                report = {
                    "triggered": False,
                    "reason": "thread_start_error",
                    "error": f"{type(exc).__name__}:{exc}",
                }
                self._record_failure(report)
                return report
        return {"triggered": True, "reason": snapshot.reason, "_thread": thread}

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                **copy.deepcopy(self._last_report),
                "running": self._thread is not None,
                "pending": self._pending is not None,
                "stopping": self._stopping,
            }

    def stop(self) -> None:
        """Request shutdown; a blocking provider is never force-killed."""

        with self._lock:
            self._stopping = True
            self._pending = None
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            try:
                thread.join(timeout=1.0)
            except Exception as exc:  # noqa: BLE001 - shutdown remains best effort
                self._record_failure(
                    {"status": "partial", "stage": "stop", "error": f"{type(exc).__name__}:{exc}"}
                )

    def _run_loop(self, snapshot: _BackgroundSnapshot) -> None:
        current = snapshot
        while True:
            report: dict[str, object] = {
                "status": "ok",
                "reason": current.reason,
                "as_of": current.now.isoformat(),
                "errors": [],
            }
            try:
                report["mature"] = self.runtime.mature_pending(
                    current.now,
                    bars_by_symbol=_clone(current.bars_by_symbol),
                )
            except Exception as exc:  # noqa: BLE001 - runtime is shadow-only even if its contract breaks
                self._background_error(report, stage="mature", error=exc)
            try:
                report["capture"] = self._capture_and_predict(current)
            except Exception as exc:  # noqa: BLE001 - still attempt capture after a maturity failure
                self._background_error(report, stage="capture", error=exc)
            if report["errors"]:
                report["status"] = "partial"
            try:
                completed_report = copy.deepcopy(report)
            except Exception as exc:  # noqa: BLE001 - exotic collaborator output stays isolated
                self._background_error(report, stage="report_copy", error=exc)
                completed_report = {
                    "status": "partial",
                    "reason": current.reason,
                    "as_of": current.now.isoformat(),
                    "errors": list(report["errors"]),
                }

            with self._lock:
                self._last_report = completed_report
                if self._stopping or self._pending is None:
                    if self._thread is threading.current_thread():
                        self._thread = None
                    return
                current = self._pending
                self._pending = None

    def _capture_and_predict(self, snapshot: _BackgroundSnapshot) -> object:
        """Call the canonical service with now; keep legacy doubles without now.

        Signature inspection stays in this runtime adapter.  The application
        service always receives an explicit snapshot clock when it accepts one.
        """

        fn = self.runtime.capture_and_predict
        episodes = _clone(snapshot.episodes)
        try:
            signature = inspect.signature(fn)
        except (TypeError, ValueError):
            return fn(episodes, now=snapshot.now)
        for args, kwargs in (
            ((episodes,), {"now": snapshot.now}),
            ((episodes,), {}),
        ):
            try:
                signature.bind(*args, **kwargs)
            except TypeError:
                continue
            return fn(*args, **kwargs)
        return fn(episodes, now=snapshot.now)

    def _background_error(self, report: dict[str, object], *, stage: str, error: Exception) -> None:
        item = {"stage": stage, "error": f"{type(error).__name__}:{error}"}
        errors = report.get("errors")
        if isinstance(errors, list):
            errors.append(item)
        self._warn(item)

    def _record_failure(self, report: dict[str, object]) -> None:
        self._last_report = copy.deepcopy(report)
        self._warn(report)

    def _warn(self, payload: object) -> None:
        try:
            warning = getattr(self.log, "warning", None)
            if callable(warning):
                warning("[world_model_shadow] %s", payload)
        except Exception:
            pass


class CallableWorldBarProvider:
    """Adapt a ``get_bars(symbol, lookback, interval)`` callable to the port."""

    def __init__(self, get_bars: object) -> None:
        if not callable(get_bars):
            raise TypeError("bar_provider must be callable")
        self._get_bars = get_bars

    def get_bars(
        self,
        symbol: str,
        lookback: str = "5d",
        interval: str = "1h",
    ) -> object:
        return self._get_bars(symbol, lookback=lookback, interval=interval)


class CallableWorldLabeler:
    """Adapt a ``label_horizon`` function or module/object to the application port."""

    def __init__(self, labeler: object) -> None:
        method = getattr(labeler, "label_horizon", None)
        if callable(method):
            self._label_horizon = method
        elif callable(labeler):
            self._label_horizon = labeler
        else:
            raise TypeError("labeler must expose label_horizon or be callable")

    def label_horizon(
        self,
        episode: object,
        bars: object,
        horizon: str,
        *,
        now: datetime,
    ) -> object:
        return self._label_horizon(episode, bars, horizon, now=now)


def _adapt_world_labeler(labeler: object | None) -> object | None:
    if labeler is None:
        return None
    method = getattr(labeler, "label_horizon", None)
    if callable(method):
        return labeler
    return CallableWorldLabeler(labeler)


def _adapt_world_bar_provider(provider: object | None) -> object | None:
    if provider is None:
        return None
    get_bars = getattr(provider, "get_bars", None)
    if callable(get_bars):
        return provider
    return CallableWorldBarProvider(provider)


class WorldModelRuntime(WorldModelService):
    """Composition adapter: wrap legacy labeler/bar shapes onto explicit ports."""

    def __init__(
        self,
        *,
        store: object,
        predictor: object | None = None,
        predictors: Iterable[object] | None = None,
        labeler: object | None = None,
        bar_provider: object | None = None,
        horizons: Iterable[str] = DEFAULT_HORIZONS,
        logger: object | None = None,
        lookback: str = "5d",
        run_id: str = "world_shadow.v1",
    ) -> None:
        super().__init__(
            store=store,
            predictor=predictor,
            predictors=predictors,
            labeler=_adapt_world_labeler(labeler),
            bar_provider=_adapt_world_bar_provider(bar_provider),
            horizons=horizons,
            logger=logger,
            lookback=lookback,
            run_id=run_id,
        )


WorldModelShadowRuntime = WorldModelRuntime
WorldModelRunner = WorldModelRuntime


__all__ = [
    "DEFAULT_HORIZONS",
    "CallableWorldBarProvider",
    "CallableWorldLabeler",
    "WorldModelBackgroundRunner",
    "WorldModelRunner",
    "WorldModelRuntime",
    "WorldModelShadowRuntime",
]
