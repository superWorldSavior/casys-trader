from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_mandate_rend_max_hold_conditionnel() -> None:
    mandate = (ROOT / "mandate" / "mandate.md").read_text(encoding="utf-8")
    low = mandate.lower()

    assert "max_hold_minutes" in mandate
    assert "optionnel" in low
    assert "expiration temporelle" in low
    assert "temps maximum de détention" not in low
    assert "autant que possible" not in low
