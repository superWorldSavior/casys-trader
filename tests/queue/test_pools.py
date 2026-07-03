import threading
import time

from trader.queue.pools import ResourcePools


def test_try_acquire_bounds_and_free_resources():
    pools = ResourcePools({"acpx": 2, "ib": 1})
    assert set(pools.free_resources()) == {"acpx", "ib"}
    assert pools.try_acquire("acpx") is True
    assert pools.try_acquire("acpx") is True
    assert pools.try_acquire("acpx") is False        # limite 2 atteinte
    assert "acpx" not in pools.free_resources()
    pools.release("acpx")
    assert "acpx" in pools.free_resources()


def test_aimd_backpressure():
    """Halving AIMD + cooldown COOLDOWN_SUCCESSES avant reprise d'incrémentation."""
    pools = ResourcePools({"acpx": 4})
    pools.on_overload("acpx")                          # 4 -> 2
    assert pools.effective_limit("acpx") == 2
    pools.on_overload("acpx")                          # 2 -> 1
    assert pools.effective_limit("acpx") == 1
    pools.on_overload("acpx")                          # plancher 1
    assert pools.effective_limit("acpx") == 1
    # COOLDOWN_SUCCESSES=3 : les deux premiers succès n'incrémentent pas encore.
    pools.on_success("acpx")                           # streak=1, M=1
    pools.on_success("acpx")                           # streak=2, M=1
    assert pools.effective_limit("acpx") == 1          # toujours en cooldown
    pools.on_success("acpx")                           # streak=3, M=2  (seuil atteint)
    pools.on_success("acpx")                           # streak=4, M=3
    pools.on_success("acpx")                           # streak=5, M=4  (plafond)
    assert pools.effective_limit("acpx") == 4


def test_over_release_is_safe():
    """release sans acquire préalable ne lève pas et ne descend pas _used sous 0."""
    pools = ResourcePools({"acpx": 1})
    # _used["acpx"] == 0 → over-release ignoré
    pools.release("acpx")
    assert pools._used["acpx"] == 0


# ── FIX 2 : cooldown explicite ─────────────────────────────────────────────

def test_cooldown_blocks_recovery_after_overload():
    """Après on_overload, il faut exactement COOLDOWN_SUCCESSES succès consécutifs
    avant que _eff recommence à monter."""
    pools = ResourcePools({"acpx": 4})
    pools.on_overload("acpx")                          # 4 → 2, streak=0
    assert pools.effective_limit("acpx") == 2

    # Les 2 premiers succès n'incrémentent pas.
    pools.on_success("acpx")                           # streak=1
    pools.on_success("acpx")                           # streak=2
    assert pools.effective_limit("acpx") == 2          # M toujours 2

    # Au 3ᵉ succès (seuil), _eff passe à 3.
    pools.on_success("acpx")                           # streak=3
    assert pools.effective_limit("acpx") == 3


def test_overload_resets_streak():
    """Un overload intermédiaire remet le streak à zéro."""
    pools = ResourcePools({"acpx": 4})
    pools.on_overload("acpx")                          # 4→2, streak=0
    pools.on_success("acpx")                           # streak=1
    pools.on_success("acpx")                           # streak=2
    # Nouvel overload avant d'atteindre le seuil.
    pools.on_overload("acpx")                          # 2→1, streak=0 à nouveau
    # Il faut encore 3 succès pour remonter.
    pools.on_success("acpx")                           # streak=1
    pools.on_success("acpx")                           # streak=2
    assert pools.effective_limit("acpx") == 1          # cooldown pas encore atteint
    pools.on_success("acpx")                           # streak=3 → +1
    assert pools.effective_limit("acpx") == 2


# ── FIX 3 : Condition + wait_for_free ──────────────────────────────────────

def test_wait_for_free_wakes_on_release():
    """Un thread bloqué dans wait_for_free se réveille dès qu'un autre release."""
    pools = ResourcePools({"acpx": 1})
    pools.try_acquire("acpx")                          # sature la ressource

    woken = threading.Event()

    def waiter():
        pools.wait_for_free(timeout=5.0)
        woken.set()

    t = threading.Thread(target=waiter, daemon=True)
    t.start()

    # Attente courte pour laisser le thread entrer dans wait_for.
    # On utilise un timeout de 5s dans wait_for_free pour que le test ne
    # pende pas en cas de bug — la vérification est sur woken.wait().
    time.sleep(0.05)

    pools.release("acpx")                             # doit réveiller le waiter

    assert woken.wait(timeout=2.0), "wait_for_free aurait dû se réveiller après release"
    t.join(timeout=2.0)


def test_wait_for_free_returns_immediately_if_resource_available():
    """Pas de blocage quand une ressource est déjà libre."""
    pools = ResourcePools({"acpx": 1})
    # aucune acquisition → acpx libre
    pools.wait_for_free(timeout=0.1)                  # ne doit pas bloquer


# ── FIX 4 : _used > _eff post-overload ─────────────────────────────────────

def test_used_exceeds_eff_post_overload():
    """Après on_overload, _used peut dépasser _eff ; les slots restent indisponibles
    jusqu'à ce qu'assez de release aient ramené _used < _eff."""
    pools = ResourcePools({"acpx": 4})

    # Acquérir les 4 permits (limite initiale = 4).
    for _ in range(4):
        assert pools.try_acquire("acpx") is True

    # Overload : _eff descend à 2, mais _used reste à 4.
    pools.on_overload("acpx")
    assert pools.effective_limit("acpx") == 2
    assert pools._used["acpx"] == 4

    # _used(4) > _eff(2) → ressource indisponible.
    assert "acpx" not in pools.free_resources()
    assert pools.try_acquire("acpx") is False

    # Libérer 2 → _used=2 == _eff=2 → toujours indisponible.
    pools.release("acpx")
    pools.release("acpx")
    assert pools._used["acpx"] == 2
    assert "acpx" not in pools.free_resources()
    assert pools.try_acquire("acpx") is False

    # Libérer 1 de plus → _used=1 < _eff=2 → acquérable.
    pools.release("acpx")
    assert "acpx" in pools.free_resources()
    assert pools.try_acquire("acpx") is True
