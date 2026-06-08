import json

from trader.cli import main


def test_cli_semantic_describe_json(capsys) -> None:
    main(["semantic", "describe", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert out["levels"][0] == "market"
    assert "indices" in out["families"]


def test_cli_indicators_list_json(capsys) -> None:
    main(["indicators", "list", "--json"])

    out = json.loads(capsys.readouterr().out)
    names = {item["name"] for item in out["indicators"]}
    assert "z_score" in names
    assert "efficiency_ratio" in names


def test_cli_semantic_describe_expose_axes_temporels(capsys) -> None:
    main(["semantic", "describe", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert "4h" in out["timeframes"]
    assert out["timeframes"]["4h"]["source_interval"] == "1h"
    assert out["query_shapes"]["indicator_get"]["axes"] == ["symbol", "indicator", "timeframe", "lookback", "window", "as_of"]


def test_cli_preserve_top_level_daemon_flags(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_daemon(argv):
        calls.append(argv)

    monkeypatch.setattr("trader.cli.daemon.main", fake_daemon)

    main(["--once"])

    assert calls == [["--once"]]


def test_cli_preserve_bootstrap_all_daemon_flag(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_daemon(argv):
        calls.append(argv)

    monkeypatch.setattr("trader.cli.daemon.main", fake_daemon)

    main(["--bootstrap-all"])

    assert calls == [["--bootstrap-all"]]


def test_cli_preserve_top_level_ib_daemon_flags(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_daemon(argv):
        calls.append(argv)

    monkeypatch.setattr("trader.cli.daemon.main", fake_daemon)

    main(["--ib-host", "10.0.0.2", "--ib-port", "4003", "--ib-client-id", "44", "--once"])

    assert calls == [["--ib-host", "10.0.0.2", "--ib-port", "4003", "--ib-client-id", "44", "--once"]]


def test_cli_preserve_top_level_consolidator_daemon_flags(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_daemon(argv):
        calls.append(argv)

    monkeypatch.setattr("trader.cli.daemon.main", fake_daemon)

    main(
        [
            "--consolidator-acpx-agent",
            "codex",
            "--consolidator-model",
            "gpt-5.5[high]",
            "--learning-consolidation-threshold",
            "50",
            "--once",
        ]
    )

    assert calls == [
        [
            "--consolidator-acpx-agent",
            "codex",
            "--consolidator-model",
            "gpt-5.5[high]",
            "--learning-consolidation-threshold",
            "50",
            "--once",
        ]
    ]
