from __future__ import annotations

from pathlib import Path

from trader.runtime import queue_runtime


class RecordingLogger:
    def __init__(self) -> None:
        self.infos: list[tuple] = []
        self.warnings: list[tuple] = []

    def info(self, *args: object) -> None:
        self.infos.append(args)

    def warning(self, *args: object) -> None:
        self.warnings.append(args)


class FakeLedger:
    instances: list["FakeLedger"] = []

    def __init__(self, path_or_db: object) -> None:
        self.path_or_db = path_or_db
        self.path = getattr(path_or_db, "path", path_or_db)
        self.recovered_at: list[int] = []
        FakeLedger.instances.append(self)

    def recover_on_boot(self, *, now_ms: int) -> None:
        self.recovered_at.append(now_ms)


class FakePools:
    instances: list["FakePools"] = []

    def __init__(self, limits: dict[str, int]) -> None:
        self.limits = limits
        FakePools.instances.append(self)


class FakePool:
    instances: list["FakePool"] = []

    def __init__(
        self,
        *,
        ledger: object,
        pools: object,
        handlers: dict[str, object],
        num_workers: int,
        now_fn,
    ) -> None:
        self.ledger = ledger
        self.pools = pools
        self.handlers = handlers
        self.num_workers = num_workers
        self.now_fn = now_fn
        self.started = False
        FakePool.instances.append(self)

    def start(self) -> None:
        self.started = True


class FakeDb:
    def __init__(self, path: Path) -> None:
        self.path = path


class FakeBroker:
    instances: list["FakeBroker"] = []

    def __init__(self, db: FakeDb, *, commission_model: object, json_path: Path) -> None:
        self.db = db
        self.commission_model = commission_model
        self.json_path = json_path
        FakeBroker.instances.append(self)


class FakePlanStore:
    instances: list["FakePlanStore"] = []

    def __init__(self, db: FakeDb, *, json_path: Path) -> None:
        self.db = db
        self.json_path = json_path
        FakePlanStore.instances.append(self)


def _reset_fakes() -> None:
    FakeLedger.instances = []
    FakePools.instances = []
    FakePool.instances = []
    FakeBroker.instances = []
    FakePlanStore.instances = []


def test_start_decide_queue_builds_ledger_pool_and_handler(tmp_path: Path) -> None:
    _reset_fakes()
    logger = RecordingLogger()
    handler_calls: list[object] = []
    client = object()

    def make_handler(*, codex_client: object) -> str:
        handler_calls.append(codex_client)
        return "decide-handler"

    runtime = queue_runtime.start_decide_queue(
        enabled=True,
        state_dir=tmp_path,
        parallelism=3,
        decision_batch_size=7,
        default_decision_batch_size=5,
        codex_client=client,
        now_ms_fn=lambda: 12345,
        logger=logger,
        factories=queue_runtime.DecideQueueFactories(
            task_ledger_cls=FakeLedger,
            resource_pools_cls=FakePools,
            decide_pool_cls=FakePool,
            make_decide_handler=make_handler,
        ),
    )

    assert runtime.enabled is True
    assert runtime.ledger is FakeLedger.instances[0]
    assert runtime.pool is FakePool.instances[0]
    assert FakeLedger.instances[0].path == tmp_path / "task_ledger.db"
    assert FakeLedger.instances[0].recovered_at == [12345]
    assert FakePools.instances[0].limits == {"acpx": 3}
    assert FakePool.instances[0].handlers == {"decide": "decide-handler"}
    assert FakePool.instances[0].num_workers == 3
    assert FakePool.instances[0].started is True
    assert handler_calls == [client]
    assert logger.infos[0][0] == "[queue_decide] pool démarré num_workers=%d db=%s"
    assert logger.warnings[0][0].startswith("[queue_decide] CASYS_DECISION_BATCH_SIZE=%d IGNORÉ")


def test_start_execute_queue_requires_sqlite_backend(tmp_path: Path) -> None:
    _reset_fakes()
    logger = RecordingLogger()

    runtime = queue_runtime.start_execute_queue(
        enabled_raw=True,
        state_backend="json",
        state_dir=tmp_path,
        commission_model=object(),
        now_ms_fn=lambda: 12345,
        logger=logger,
        factories=queue_runtime.ExecuteQueueFactories(
            open_state_db=lambda _path: FakeDb(_path),
            sqlite_broker_cls=FakeBroker,
            sqlite_plan_store_cls=FakePlanStore,
            task_ledger_cls=FakeLedger,
            resource_pools_cls=FakePools,
            decide_pool_cls=FakePool,
            make_execute_order_handler=lambda **_kwargs: "handler",
        ),
    )

    assert runtime.enabled is False
    assert runtime.ledger is None
    assert runtime.pool is None
    assert FakeLedger.instances == []
    assert logger.warnings == [
        ("[queue_execute] CASYS_QUEUE_EXECUTE_ENABLED=1 ignoré — requiert CASYS_STATE_BACKEND=sqlite",)
    ]


def test_start_execute_queue_builds_shared_sqlite_stack(tmp_path: Path) -> None:
    _reset_fakes()
    logger = RecordingLogger()
    handler_kwargs: list[dict] = []
    opened_paths: list[Path] = []
    commission_model = object()

    def open_db(path: Path) -> FakeDb:
        opened_paths.append(path)
        return FakeDb(path)

    def make_handler(**kwargs: object) -> str:
        handler_kwargs.append(kwargs)
        return "execute-handler"

    runtime = queue_runtime.start_execute_queue(
        enabled_raw=True,
        state_backend="sqlite",
        state_dir=tmp_path,
        commission_model=commission_model,
        now_ms_fn=lambda: 98765,
        logger=logger,
        factories=queue_runtime.ExecuteQueueFactories(
            open_state_db=open_db,
            sqlite_broker_cls=FakeBroker,
            sqlite_plan_store_cls=FakePlanStore,
            task_ledger_cls=FakeLedger,
            resource_pools_cls=FakePools,
            decide_pool_cls=FakePool,
            make_execute_order_handler=make_handler,
        ),
    )

    db = runtime.db
    assert runtime.enabled is True
    assert db is not None
    assert opened_paths == [tmp_path / "casys.db"]
    assert FakeBroker.instances[0].db is db
    assert FakeBroker.instances[0].commission_model is commission_model
    assert FakeBroker.instances[0].json_path == tmp_path / "broker.json"
    assert FakePlanStore.instances[0].db is db
    assert FakePlanStore.instances[0].json_path == tmp_path / "trade_plans.json"
    assert runtime.ledger is FakeLedger.instances[0]
    assert FakeLedger.instances[0].path_or_db is db
    assert FakeLedger.instances[0].recovered_at == [98765]
    assert FakePools.instances[0].limits == {"portfolio": 1}
    assert runtime.pool is FakePool.instances[0]
    assert FakePool.instances[0].handlers == {"execute_order": "execute-handler"}
    assert FakePool.instances[0].num_workers == 1
    assert FakePool.instances[0].started is True
    assert handler_kwargs == [
        {
            "db": db,
            "broker": FakeBroker.instances[0],
            "plan_store": FakePlanStore.instances[0],
            "ledger": FakeLedger.instances[0],
        }
    ]
    assert logger.infos == [("[queue_execute] pool démarré db=%s", FakeLedger.instances[0].path)]
