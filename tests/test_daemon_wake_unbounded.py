"""Le réveil demandé par l'agent est respecté sans borne (défaut None/None).

Le clamp historique (min 5 / max 240 min) était arbitraire ; l'agent est
autonome sur sa cadence de re-décision (les sorties restent vérifiées à chaque
poll, cf. _apply_planned_exits appelé à chaque cycle). Décision Erwan 2026-07-02.
"""
from __future__ import annotations

from trader.daemon import _bounded_wake_minutes


def test_wake_non_borne_par_defaut():
    # 260 min (ex-clampé à 240), 7 jours, 1 min : tout passe intact.
    assert _bounded_wake_minutes(260.0) == 260.0
    assert _bounded_wake_minutes(10080.0) == 10080.0
    assert _bounded_wake_minutes(1.0) == 1.0


def test_wake_borne_seulement_si_flag_fourni():
    # Les flags --min/--max réactivent un bornage explicite si l'opérateur le veut.
    assert _bounded_wake_minutes(260.0, maximum=240.0) == 240.0
    assert _bounded_wake_minutes(1.0, minimum=5.0) == 5.0
    assert _bounded_wake_minutes(100.0, minimum=5.0, maximum=240.0) == 100.0
