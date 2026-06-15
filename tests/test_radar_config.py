from trader.radar_config import load_radar_params


def test_defaults_and_overrides(tmp_path) -> None:
    (tmp_path / "radar.yaml").write_text(
        "cap_m: 25\n"
        "delta: 0.05\n"
        "dwell_days: 3\n"
        "atr_floor: 0.01\n"
        "amplitude_cap: 0.05\n"
        "min_coverage: 0.9\n"
        "benchmarks: {US: SPY, EU: ^FCHI}\n",
        encoding="utf-8",
    )

    params = load_radar_params(tmp_path)

    assert params.cap_m == 25
    assert params.dwell_days == 3
    assert params.atr_floor == 0.01
    assert params.amplitude_cap == 0.05
    assert params.min_coverage == 0.9
    assert params.benchmarks["EU"] == "^FCHI"


def test_missing_file_yields_documented_defaults(tmp_path) -> None:
    params = load_radar_params(tmp_path)

    assert params.cap_m == 25
    assert params.min_coverage == 0.8
