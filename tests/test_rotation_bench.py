"""Tests pour trader.rotation.bench — mesure de l'apport P&L du hot-set."""

from __future__ import annotations

import pytest

from trader.rotation.bench import membership_pnl


def test_membership_pnl_cas_nominal():
    """Cas de base décrit dans le brief."""
    trades = [("A", 10.0), ("A", -3.0), ("B", 50.0)]
    result = membership_pnl(trades, hot_set={"A"})
    assert result["in_hot_set_pnl"] == pytest.approx(7.0)
    assert result["out_hot_set_pnl"] == pytest.approx(50.0)
    assert result["in_hot_set_trades"] == 2
    assert result["out_hot_set_trades"] == 1


def test_membership_pnl_hot_set_vide():
    """hot_set vide : tous les trades en out."""
    trades = [("A", 5.0), ("B", -2.0)]
    result = membership_pnl(trades, hot_set=set())
    assert result["in_hot_set_pnl"] == pytest.approx(0.0)
    assert result["out_hot_set_pnl"] == pytest.approx(3.0)
    assert result["in_hot_set_trades"] == 0
    assert result["out_hot_set_trades"] == 2


def test_membership_pnl_tous_in_hot_set():
    """Tous les symboles appartiennent au hot_set."""
    trades = [("A", 1.0), ("B", 2.0)]
    result = membership_pnl(trades, hot_set={"A", "B"})
    assert result["in_hot_set_pnl"] == pytest.approx(3.0)
    assert result["out_hot_set_pnl"] == pytest.approx(0.0)
    assert result["in_hot_set_trades"] == 2
    assert result["out_hot_set_trades"] == 0


def test_membership_pnl_trades_vides():
    """Liste vide : tout à zéro."""
    result = membership_pnl([], hot_set={"A"})
    assert result["in_hot_set_pnl"] == pytest.approx(0.0)
    assert result["out_hot_set_pnl"] == pytest.approx(0.0)
    assert result["in_hot_set_trades"] == 0
    assert result["out_hot_set_trades"] == 0


def test_membership_pnl_retourne_toutes_les_cles():
    """La structure de retour contient exactement les 4 clés attendues."""
    result = membership_pnl([("X", 1.0)], hot_set={"X"})
    assert set(result.keys()) == {
        "in_hot_set_pnl",
        "out_hot_set_pnl",
        "in_hot_set_trades",
        "out_hot_set_trades",
    }
