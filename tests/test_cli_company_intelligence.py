from trader.runtime import cli


def test_company_intelligence_cli_exposes_refresh_and_status() -> None:
    refresh = cli.build_parser().parse_args(
        [
            "company-intelligence",
            "refresh",
            "--scope",
            "active",
            "--depth",
            "deep",
            "--symbol",
            "AAPL",
            "--wait",
        ]
    )
    status = cli.build_parser().parse_args(["company-intelligence", "status"])

    assert refresh.func is cli._cmd_company_intelligence_refresh
    assert refresh.scope == "active"
    assert refresh.depth == "deep"
    assert refresh.symbol == ["AAPL"]
    assert refresh.wait is True
    assert status.func is cli._cmd_company_intelligence_status
