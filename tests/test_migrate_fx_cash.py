import json
from pathlib import Path
import pytest
from scripts import migrate_fx_cash


def _broker(tmp_path: Path) -> Path:
    p = tmp_path / "broker.json"
    p.write_text(json.dumps({
        "cash": 100000.0 - 8700.0 - 80.0,  # cash pollué (TWD brut déduit)
        "positions": {},
        "fills": [
            {"symbol": "2379.TW", "side": "BUY", "quantity": 10.0, "price": 870.0,
             "ts": "2026-06-23T05:00:00+00:00", "commission": 80.0,
             "commission_currency": "TWD"},
        ],
    }))
    return p


def test_dry_run_does_not_mutate(tmp_path):
    p = _broker(tmp_path)
    before = p.read_text()
    report = migrate_fx_cash.run(p, rates={"TWD": 0.031}, starting_cash=100000.0, commit=False)
    assert p.read_text() == before
    assert "cash_before" in report and "cash_after" in report


def test_commit_recomputes_cash_usd(tmp_path):
    p = _broker(tmp_path)
    migrate_fx_cash.run(p, rates={"TWD": 0.031}, starting_cash=100000.0, commit=True)
    data = json.loads(p.read_text())
    # cash USD = 100000 - (8700+80)*0.031
    assert data["cash"] == pytest.approx(100000.0 - 8780.0 * 0.031)
    assert data["fills"][0]["fx_rate"] == 0.031
    assert (p.parent / (p.name + ".bak-pre-fx")).exists() or any(
        f.name.startswith("broker.json.bak-pre-fx") for f in p.parent.iterdir())
