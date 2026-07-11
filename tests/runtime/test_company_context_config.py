from trader.runtime.company_context_config import load_company_context_projection_limits


def test_projection_limits_read_company_intelligence_config(tmp_path) -> None:
    (tmp_path / "company_intelligence.yaml").write_text(
        "projection:\n  summary_chars_per_symbol: 111\n  max_points_per_symbol: 3\n",
        encoding="utf-8",
    )

    limits = load_company_context_projection_limits(tmp_path)

    assert limits.summary_chars_per_symbol == 111
    assert limits.max_points_per_symbol == 3
