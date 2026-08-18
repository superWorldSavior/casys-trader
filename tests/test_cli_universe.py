from trader.runtime import cli


def test_universe_cli_exposes_repeatable_venues_force_and_no_macro() -> None:
    refresh = cli.build_parser().parse_args(
        [
            "universe",
            "refresh",
            "--venue",
            "US",
            "--venue",
            "TW",
            "--force",
            "--no-macro",
        ]
    )

    assert refresh.func is cli._cmd_universe_refresh
    assert refresh.venue == ["US", "TW"]
    assert refresh.all is False
    assert refresh.force is True
    assert refresh.no_macro is True


def test_universe_cli_runs_refresh_helper_with_force(monkeypatch, tmp_path, capsys) -> None:
    calls = []

    def fake_refresh(**kwargs):
        calls.append(kwargs)
        return {
            "prepared": [{"venue": "US"}],
            "waiting": [],
            "skipped": [],
            "errors": [],
            "macro_refreshed": [{"venue": "US"}],
        }

    monkeypatch.setattr(cli.daemon, "ROOT", tmp_path)
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(
        cli.universe_intelligence_runtime,
        "refresh_universe_intelligence",
        fake_refresh,
    )

    assert cli.main(["universe", "refresh", "--venue", "US", "--force"]) == 0
    assert calls[0]["venues"] == ("US",)
    assert calls[0]["force"] is True
    assert calls[0]["refresh_macro"] is True
    out = capsys.readouterr().out
    assert '"requested_venues": [\n    "US"\n  ]' in out
    assert '"force": true' in out


def test_universe_cli_no_macro_disables_brief_refresh(monkeypatch, tmp_path) -> None:
    calls = []

    def fake_refresh(**kwargs):
        calls.append(kwargs)
        return {"prepared": [], "waiting": [], "skipped": [], "errors": [], "macro_refreshed": []}

    monkeypatch.setattr(cli.daemon, "ROOT", tmp_path)
    monkeypatch.setattr(cli.daemon, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(
        cli.universe_intelligence_runtime,
        "refresh_universe_intelligence",
        fake_refresh,
    )

    assert cli.main(["universe", "refresh", "--venue", "US", "--force", "--no-macro"]) == 0
    assert calls[0]["refresh_macro"] is False
