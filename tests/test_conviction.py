import pytest

from trader.market.radar_config import ConvictionError, load_conviction
from trader.semantic.catalog import FAMILIES


def test_empty_default(tmp_path) -> None:
    assert load_conviction(tmp_path, known_families=set(FAMILIES)) == {}


def test_valid_tilt(tmp_path) -> None:
    (tmp_path / "conviction.yaml").write_text("energy: 0.30\n", encoding="utf-8")

    assert load_conviction(tmp_path, known_families=set(FAMILIES)) == {"energy": 0.30}


def test_unknown_family_rejected(tmp_path) -> None:
    (tmp_path / "conviction.yaml").write_text("zzz: 0.1\n", encoding="utf-8")

    with pytest.raises(ConvictionError):
        load_conviction(tmp_path, known_families=set(FAMILIES))


def test_tilt_le_minus_one_rejected(tmp_path) -> None:
    (tmp_path / "conviction.yaml").write_text("energy: -1.0\n", encoding="utf-8")

    with pytest.raises(ConvictionError):
        load_conviction(tmp_path, known_families=set(FAMILIES))
