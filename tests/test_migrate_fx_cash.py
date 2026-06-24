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


def test_commission_converted_via_own_currency(tmp_path):
    """Regression: commission en EUR sur un fill TWD doit être converti à 1.2 (EUR), pas 0.031 (TWD).

    Case: symbol priced in TWD (rate=0.031), but commission billed in EUR (rate=1.2).
    The buggy code reuses the symbol's rate (0.031) for the commission.
    The fixed code must look up EUR in rates separately.
    """
    p = tmp_path / "broker.json"
    p.write_text(json.dumps({
        "cash": 0.0,
        "positions": {},
        "fills": [
            {
                "symbol": "2379.TW",        # TWD-priced symbol
                "side": "BUY",
                "quantity": 1.0,
                "price": 100.0,              # trade value = 100 TWD → 3.1 USD
                "ts": "2026-06-24T05:00:00+00:00",
                "commission": 10.0,
                "commission_currency": "EUR",  # commission in EUR, NOT TWD
            },
        ],
    }))
    rates = {"TWD": 0.031, "EUR": 1.2}
    report = migrate_fx_cash.run(p, rates=rates, starting_cash=100.0, commit=False)
    # trade value: 100 TWD * 0.031 = 3.1 USD deducted
    # commission: 10 EUR * 1.2 = 12.0 USD deducted  (NOT 10 * 0.031 = 0.31)
    expected_cash = 100.0 - 3.1 - 12.0
    buggy_cash = 100.0 - 3.1 - 0.31  # what the buggy code produces
    assert report["cash_after"] != pytest.approx(buggy_cash, rel=1e-4), (
        "Bug still present: commission was converted at symbol rate (TWD=0.031) instead of EUR rate (1.2)."
    )
    assert report["cash_after"] == pytest.approx(expected_cash, rel=1e-6), (
        f"Expected {expected_cash} but got {report['cash_after']}. "
        "Commission must be converted at its own currency rate (EUR=1.2), not symbol rate (TWD=0.031)."
    )


def _perf(tmp_path: Path) -> Path:
    p = tmp_path / "model_performance.jsonl"
    p.write_text(
        json.dumps({"symbol": "2379.TW", "action": "BUY", "price": 870.0, "quantity": 10.0,
                    "commission": 80.0, "commission_currency": "TWD", "ts": "2026-06-23T05:00:00+00:00"}) + "\n"
        + json.dumps({"symbol": "MSFT", "action": "BUY", "price": 100.0, "quantity": 2.0,
                      "ts": "2026-06-23T06:00:00+00:00"}) + "\n"
    )
    return p


def test_stamp_perf_dry_run_does_not_mutate(tmp_path):
    p = _perf(tmp_path)
    before = p.read_text()
    rep = migrate_fx_cash.stamp_perf_rows(p, rates={"TWD": 0.031}, commit=False)
    assert p.read_text() == before
    # TWD stampé à 0.031, USD (MSFT) stampé explicitement à 1.0 (déterminisme)
    assert rep["stamped"] == 2 and rep["by_ccy"] == {"TWD": 1, "USD": 1}


def test_stamp_perf_commit_stamps_fx_rate(tmp_path):
    p = _perf(tmp_path)
    migrate_fx_cash.stamp_perf_rows(p, rates={"TWD": 0.031}, commit=True)
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    tw = next(r for r in rows if r["symbol"] == "2379.TW")
    assert tw["fx_rate"] == 0.031
    assert any(f.name.startswith("model_performance.jsonl.bak-pre-fx") for f in p.parent.iterdir())


def test_stamp_perf_idempotent(tmp_path):
    p = _perf(tmp_path)
    migrate_fx_cash.stamp_perf_rows(p, rates={"TWD": 0.031}, commit=True)
    rep2 = migrate_fx_cash.stamp_perf_rows(p, rates={"TWD": 0.031}, commit=True)
    assert rep2["stamped"] == 0  # déjà stampé → rien à refaire
