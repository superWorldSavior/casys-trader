
from trader.market.gross_priority import PriorityItem, gross_execution_order


def _order(triples):
    return gross_execution_order([PriorityItem(*t) for t in triples])


def test_reducteurs_avant_ouvertures() -> None:
    order = _order([("OPEN", "OPEN_LONG", 0.9), ("RED", "REDUCE", 0.1), ("CLO", "CLOSE", 0.0)])
    assert order.index("RED") < order.index("OPEN")
    assert order.index("CLO") < order.index("OPEN")


def test_ouvertures_par_conviction_decroissante() -> None:
    order = _order([("LOW", "OPEN_LONG", 0.3), ("HIGH", "OPEN_SHORT", 0.9), ("MID", "OPEN_LONG", 0.6)])
    assert order == ["HIGH", "MID", "LOW"]


def test_reste_apres_les_ouvertures() -> None:
    order = _order([("H", "HOLD", 0.0), ("O", "OPEN_LONG", 0.5)])
    assert order.index("O") < order.index("H")


def test_tie_break_symbole_deterministe() -> None:
    order = _order([("ZZZ", "OPEN_LONG", 0.7), ("AAA", "OPEN_LONG", 0.7)])
    assert order == ["AAA", "ZZZ"]


def test_ordre_complet_trois_phases() -> None:
    items = [
        ("o_low", "OPEN_LONG", 0.2),
        ("hold", "HOLD", 0.0),
        ("red", "REDUCE", 0.0),
        ("o_high", "OPEN_SHORT", 0.95),
        ("close", "CLOSE", 0.0),
    ]
    # phase 0 réducteurs (close < red par symbole), phase 1 ouvertures (o_high puis
    # o_low), phase 2 reste (hold)
    assert _order(items) == ["close", "red", "o_high", "o_low", "hold"]


def test_determinisme_independant_de_lordre_dentree() -> None:
    items = [("A", "OPEN_LONG", 0.5), ("B", "REDUCE", 0.0), ("C", "OPEN_LONG", 0.8)]
    assert _order(items) == _order(list(reversed(items)))


def test_confidence_non_finie_traitee_comme_basse_et_deterministe() -> None:
    # En mode paper le gate de confiance est off : une confidence NaN/inf peut
    # atteindre le tri. Les comparaisons NaN étant toutes fausses, un tri naïf
    # redeviendrait dépendant de l'ordre d'entrée. On exige : déterministe
    # (invariant par permutation) ET conviction non finie traitée comme la plus
    # basse (servie après les ouvertures à conviction finie).
    items = [
        ("nan1", "OPEN_LONG", float("nan")),
        ("good", "OPEN_LONG", 0.5),
        ("nan2", "OPEN_SHORT", float("inf")),
    ]
    o1 = _order(items)
    o2 = _order(list(reversed(items)))
    assert o1 == o2
    assert o1[0] == "good"
    assert set(o1[1:]) == {"nan1", "nan2"}
