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
    pools = ResourcePools({"acpx": 4})
    pools.on_overload("acpx")                          # 4 -> 2
    assert pools.effective_limit("acpx") == 2
    pools.on_overload("acpx")                          # 2 -> 1
    assert pools.effective_limit("acpx") == 1
    pools.on_overload("acpx")                          # plancher 1
    assert pools.effective_limit("acpx") == 1
    pools.on_success("acpx")                           # 1 -> 2
    pools.on_success("acpx"); pools.on_success("acpx")  # 3, 4
    pools.on_success("acpx")                           # plafond = limite initiale 4
    assert pools.effective_limit("acpx") == 4
