"""Mesure de l'apport P&L du hot-set de rotation.

Compare le P&L des trades selon que le symbole appartient ou non
au hot-set actif, pour évaluer la valeur ajoutée de la sélection d'univers.
"""

from __future__ import annotations


def membership_pnl(
    trades: list[tuple[str, float]],
    *,
    hot_set: set[str],
) -> dict[str, float | int]:
    """Décompose le P&L selon l'appartenance au hot-set.

    Args:
        trades: liste de tuples (symbol, pnl_float).
        hot_set: ensemble de symboles considérés « chauds ».

    Returns:
        dict avec les clés :
            - in_hot_set_pnl : somme des pnl pour symboles ∈ hot_set
            - out_hot_set_pnl : somme des pnl pour symboles ∉ hot_set
            - in_hot_set_trades : nombre de trades ∈ hot_set
            - out_hot_set_trades : nombre de trades ∉ hot_set
    """
    in_pnl = 0.0
    out_pnl = 0.0
    in_count = 0
    out_count = 0

    for symbol, pnl in trades:
        if symbol in hot_set:
            in_pnl += pnl
            in_count += 1
        else:
            out_pnl += pnl
            out_count += 1

    return {
        "in_hot_set_pnl": in_pnl,
        "out_hot_set_pnl": out_pnl,
        "in_hot_set_trades": in_count,
        "out_hot_set_trades": out_count,
    }
