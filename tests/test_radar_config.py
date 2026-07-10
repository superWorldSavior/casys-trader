from trader.market.radar_config import RadarParams, load_radar_params


def test_gap_threshold_default() -> None:
    """RadarParams a gap_threshold = 0.03 par défaut."""
    params = RadarParams()
    assert params.gap_threshold == 0.03


def test_gap_threshold_loaded_from_yaml(tmp_path) -> None:
    """gap_threshold est chargé depuis radar.yaml."""
    (tmp_path / "radar.yaml").write_text("gap_threshold: 0.05\n", encoding="utf-8")
    params = load_radar_params(tmp_path)
    assert params.gap_threshold == 0.05


def test_gap_threshold_default_when_absent_from_yaml(tmp_path) -> None:
    """Si gap_threshold absent du yaml, la valeur par défaut 0.03 est utilisée."""
    (tmp_path / "radar.yaml").write_text("cap_m: 25\n", encoding="utf-8")
    params = load_radar_params(tmp_path)
    assert params.gap_threshold == 0.03


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


# ---------------------------------------------------------------------------
# override_enabled — nouveau champ
# ---------------------------------------------------------------------------

def test_override_enabled_default_true() -> None:
    """RadarParams.override_enabled vaut True par défaut."""
    params = RadarParams()
    assert params.override_enabled is True


def test_override_enabled_loaded_from_yaml(tmp_path) -> None:
    """override_enabled=false dans radar.yaml est chargé correctement."""
    (tmp_path / "radar.yaml").write_text("override_enabled: false\n", encoding="utf-8")
    params = load_radar_params(tmp_path)
    assert params.override_enabled is False


def test_override_enabled_true_loaded_from_yaml(tmp_path) -> None:
    """override_enabled=true dans radar.yaml est chargé correctement."""
    (tmp_path / "radar.yaml").write_text("override_enabled: true\n", encoding="utf-8")
    params = load_radar_params(tmp_path)
    assert params.override_enabled is True


def test_override_enabled_default_when_absent_from_yaml(tmp_path) -> None:
    """Si override_enabled absent du yaml, la valeur par défaut True est utilisée."""
    (tmp_path / "radar.yaml").write_text("cap_m: 25\n", encoding="utf-8")
    params = load_radar_params(tmp_path)
    assert params.override_enabled is True
