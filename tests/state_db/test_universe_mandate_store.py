from trader.domain.universe import SymbolMandate, UniverseMandate
from trader.infrastructure.state_db.universe_mandate_store import UniverseMandateStore


def _prepared(scope: str = "scope-1") -> UniverseMandate:
    return UniverseMandate(
        mandate_id="mandate-1",
        candidate_scope_id=scope,
        venue="US",
        agent_run_id="run-1",
        as_of="2026-07-10T08:00:00+00:00",
        valid_until="2026-07-11T08:00:00+00:00",
        status="prepared",
        symbols={
            "EXM": SymbolMandate(
                symbol="EXM",
                why_selected="Best supported candidate.",
                role="quality_growth",
                posture="constructive",
                allowed_sides=("long",),
                company_brief_ref={"brief_id": "brief-1"},
            )
        },
        family_postures={"us_growth": "favored"},
        portfolio_posture={"gross_mode": "cautious", "net_bias": "neutral"},
    )


def test_prepared_mandate_is_invisible_until_activation(tmp_path) -> None:
    store = UniverseMandateStore(tmp_path / "mandates")
    store.write_prepared(_prepared())

    assert store.active_slice_for_symbol("EXM") is None

    active = store.activate(
        venue="US",
        candidate_scope_id="scope-1",
        as_of="2026-07-10T09:00:00+00:00",
        selected_symbols=("EXM",),
    )

    assert active["status"] == "active"
    assert store.active_slice_for_symbol("EXM")["symbol_mandate"]["why_selected"] == (
        "Best supported candidate."
    )
    assert active["family_postures"] == {"us_growth": "favored"}
    assert active["portfolio_posture"] == {"gross_mode": "cautious", "net_bias": "neutral"}
    assert store.active_slice_for_symbol("EXM")["family_postures"] == {"us_growth": "favored"}
    assert store.active_slice_for_symbol("EXM")["portfolio_posture"] == {
        "gross_mode": "cautious",
        "net_bias": "neutral",
    }


def test_fallback_activation_does_not_invent_agent_rationale(tmp_path) -> None:
    store = UniverseMandateStore(tmp_path / "mandates")

    active = store.activate(
        venue="US",
        candidate_scope_id="scope-missing",
        as_of="2026-07-10T09:00:00+00:00",
        selected_symbols=("BASE",),
        fallback_reason="prepare_missing",
    )

    assert active["status"] == "fallback"
    assert active["symbols"]["BASE"]["why_selected"] == ""
    assert active["symbols"]["BASE"]["posture"] == "unmandated"


def test_new_activation_removes_old_symbol_projection_for_same_venue(tmp_path) -> None:
    store = UniverseMandateStore(tmp_path / "mandates")
    store.write_prepared(_prepared())
    store.activate(
        venue="US",
        candidate_scope_id="scope-1",
        as_of="2026-07-10T09:00:00+00:00",
        selected_symbols=("EXM",),
    )
    store.activate(
        venue="US",
        candidate_scope_id="scope-2",
        as_of="2026-07-11T09:00:00+00:00",
        selected_symbols=("NEW",),
        fallback_reason="agent_error",
    )

    assert store.active_slice_for_symbol("EXM") is None
    assert store.active_slice_for_symbol("NEW") is not None
