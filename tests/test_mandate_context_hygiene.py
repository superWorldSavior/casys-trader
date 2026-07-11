from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_mandat_et_prior_ne_recopient_plus_les_contrats_runtime_obsoletes() -> None:
    mandate = (ROOT / "mandate" / "mandate.md").read_text(encoding="utf-8")
    memory = (ROOT / "mandate" / "memory.md").read_text(encoding="utf-8")
    combined = f"{mandate}\n{memory}"

    for stale in (
        "REQUEST_CONTEXT",
        "next_wake_in_minutes",
        'context["symbol"]',
        "tools/memory.py",
        "confidence_below_required",
        "max_position_value` 30",
    ):
        assert stale not in combined


def test_mandat_separe_competence_situation_et_experience() -> None:
    mandate = (ROOT / "mandate" / "mandate.md").read_text(encoding="utf-8")
    memory = (ROOT / "mandate" / "memory.md").read_text(encoding="utf-8")

    assert "company_intelligence_delta" in mandate
    assert "universe_mandate" in mandate
    assert "company_intelligence_delta" in memory
    assert "FLAIR" in mandate
    assert "prior humain lent" in memory
    assert "pas le journal runtime" in memory
    assert "config/risk.yaml" in mandate
