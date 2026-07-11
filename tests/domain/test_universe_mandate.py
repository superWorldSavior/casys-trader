from trader.domain.universe import SymbolMandate, UniverseMandate


def test_symbol_mandate_round_trip_preserves_directional_and_portfolio_context() -> None:
    mandate = SymbolMandate.from_mapping(
        "AIR.PA",
        {
            "why_selected": "Defense demand remains supportive.",
            "role": "core_candidate",
            "posture": "constructive",
            "directional_view": "long_bias",
            "portfolio_context": {
                "exposure_note": "Avoid increasing concentrated defense exposure.",
                "risk_notes": ["Earnings are approaching."],
            },
        },
    )

    assert mandate.directional_view == "long_bias"
    assert mandate.portfolio_context == {
        "exposure_note": "Avoid increasing concentrated defense exposure.",
        "risk_notes": ["Earnings are approaching."],
    }
    assert mandate.to_dict()["directional_view"] == "long_bias"
    assert mandate.to_dict()["portfolio_context"] == {
        "exposure_note": "Avoid increasing concentrated defense exposure.",
        "risk_notes": ["Earnings are approaching."],
    }


def test_universe_mandate_to_dict_exposes_family_and_portfolio_postures() -> None:
    mandate = UniverseMandate(
        mandate_id="mandate-1",
        candidate_scope_id="scope-1",
        venue="EU",
        agent_run_id="run-1",
        as_of="2026-07-11T08:00:00+00:00",
        valid_until="2026-07-12T08:00:00+00:00",
        status="prepared",
        symbols={},
        family_postures={"defense_aero_eu": "favored"},
        portfolio_posture={"gross_mode": "normal", "net_bias": "long"},
    )

    assert mandate.to_dict()["family_postures"] == {"defense_aero_eu": "favored"}
    assert mandate.to_dict()["portfolio_posture"] == {
        "gross_mode": "normal",
        "net_bias": "long",
    }


def test_symbol_mandate_drops_order_and_sizing_fields_from_advisory_context() -> None:
    mandate = SymbolMandate.from_mapping(
        "AIR.PA",
        {
            "why_selected": "Defense demand remains supportive.",
            "directional_view": "invalid",
            "qty": 100,
            "stop": 120.0,
            "portfolio_context": {
                "exposure_note": "Avoid concentration.",
                "risk_notes": ["Earnings soon."],
                "qty": 100,
                "risk_pct": 0.02,
            },
        },
    )

    serialized = mandate.to_dict()
    assert mandate.directional_view == "neutral"
    assert serialized["portfolio_context"] == {
        "exposure_note": "Avoid concentration.",
        "risk_notes": ["Earnings soon."],
    }
    assert "qty" not in serialized
    assert "stop" not in serialized
    assert "risk_pct" not in str(serialized["portfolio_context"])
