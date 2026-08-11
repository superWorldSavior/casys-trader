from trader.runtime import cli


def test_news_macro_cli_exposes_repeatable_targeted_venues_and_force() -> None:
    refresh = cli.build_parser().parse_args(
        [
            "news-macro",
            "refresh",
            "--venue",
            "TW",
            "--venue",
            "GLOBAL",
            "--force",
        ]
    )

    assert refresh.func is cli._cmd_news_macro_refresh
    assert refresh.venue == ["TW", "GLOBAL"]
    assert refresh.all is False
    assert refresh.force is True


def test_news_macro_cli_runs_only_requested_market(monkeypatch, tmp_path, capsys) -> None:
    calls = []

    def fake_tick(**kwargs):
        calls.append(kwargs)
        return {"triggered": [{"venue": "TW"}], "skipped": [], "errors": []}

    monkeypatch.setattr(cli.daemon, "ROOT", tmp_path)
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cli.news_macro_runtime, "tick_news_macro_analysis", fake_tick)

    assert cli.main(["news-macro", "refresh", "--venue", "TW", "--force"]) == 0
    assert calls[0]["venues"] == ("TW",)
    assert calls[0]["include_global"] is False
    assert calls[0]["force"] is True
    assert '"requested_venues": [\n    "TW"\n  ]' in capsys.readouterr().out
