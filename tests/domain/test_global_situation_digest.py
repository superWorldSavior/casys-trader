from trader.domain.situation import (
    DEFAULT_MAX_DIGEST_POINTS,
    GlobalSituationDigest,
    NewsMacroBrief,
    SituationPoint,
    SituationSection,
    build_global_situation_digest,
)
from trader.domain.situation.brief import MAX_POINT_CHARS


def _point(
    text: str,
    *,
    severity: str = "info",
    signal: str = "weak",
    direction: str | None = None,
    source_ref: str = "ref",
) -> SituationPoint:
    point = SituationPoint.from_mapping(
        {
            "point": text,
            "sources": ["Reuters"],
            "source_refs": [source_ref],
            "severity": severity,
            "signal": signal,
            "direction": direction,
        }
    )
    assert point is not None
    return point


def _brief(
    venue: str,
    *,
    alerts: tuple[SituationPoint, ...] = (),
    zones: tuple[SituationSection, ...] = (),
) -> NewsMacroBrief:
    return NewsMacroBrief(
        brief_id=f"2026-07-11|{venue}",
        venue=venue,
        as_of="2026-07-11T08:00:00+00:00",
        valid_until="2026-07-12T08:00:00+00:00",
        alerts=alerts,
        zones=zones,
    )


def test_build_global_digest_is_bounded_and_prioritises_macro_points() -> None:
    long_point = _point(
        "x" * (MAX_POINT_CHARS + 40),
        severity="risk",
        signal="event",
        direction="risk_off",
        source_ref="tw-risk",
    )
    regional_briefs = {
        "US": _brief(
            "US",
            alerts=(
                _point(
                    "Fed remains hawkish and the dollar is strong.",
                    severity="watch",
                    signal="strong",
                    source_ref="us-rates",
                ),
                _point("US info", source_ref="us-info"),
            ),
        ),
        "TW": _brief(
            "TW",
            alerts=(long_point, _point("TW info", source_ref="tw-info")),
        ),
        "EU": _brief(
            "EU",
            zones=(
                SituationSection(
                    name="Europe",
                    points=(
                        _point(
                            "Europe watch event",
                            severity="watch",
                            signal="event",
                            source_ref="eu-event",
                        ),
                    ),
                ),
            ),
        ),
    }

    digest = build_global_situation_digest(
        regional_briefs,
        as_of="2026-07-11T09:00:00+00:00",
        max_points=3,
    )

    assert len(digest.points) == 3
    assert len(digest.points) <= DEFAULT_MAX_DIGEST_POINTS
    assert digest.points[0] == long_point
    assert all(len(point.point) <= MAX_POINT_CHARS for point in digest.points)
    assert digest.coverage == {"venues_seen": ("EU", "TW", "US"), "point_count": 3}
    assert digest.rates_bias == "hawkish"
    assert digest.usd_bias == "strong"


def test_build_global_digest_is_deterministic() -> None:
    briefs = {
        "US": _brief(
            "US",
            alerts=(
                _point(
                    "Risk off as oil shock hits markets.",
                    severity="risk",
                    signal="event",
                    direction="risk_off",
                    source_ref="oil",
                ),
            ),
        ),
    }

    first = build_global_situation_digest(briefs, as_of="2026-07-11T09:00:00+00:00")
    second = build_global_situation_digest(briefs, as_of="2026-07-11T09:00:00+00:00")

    assert first == second
    assert first.regime == "risk_off"


def test_global_brief_points_are_prioritised_over_regional() -> None:
    """Global brief points appear before regional ones regardless of severity/signal."""
    global_point = _point(
        "Global cross-asset shift to risk-off.",
        severity="info",
        signal="weak",
        source_ref="global-ref",
    )
    regional_point = _point(
        "Regional risk event with maximum severity.",
        severity="risk",
        signal="event",
        source_ref="regional-ref",
    )
    global_brief = _brief("GLOBAL", alerts=(global_point,))
    regional_briefs = {"EU": _brief("EU", alerts=(regional_point,))}

    digest = build_global_situation_digest(
        regional_briefs,
        as_of="2026-07-11T09:00:00+00:00",
        global_brief=global_brief,
    )

    assert len(digest.points) == 2
    assert digest.points[0] == global_point
    assert digest.points[1] == regional_point


def test_global_brief_cap_is_respected_and_displaces_regional_overflow() -> None:
    """When global brief fills the cap, excess regional points are excluded."""
    global_points = tuple(
        _point(f"Global point {i}.", source_ref=f"g{i}")
        for i in range(4)
    )
    regional_point = _point("Regional point.", source_ref="r0")

    global_brief = _brief("GLOBAL", alerts=global_points)
    regional_briefs = {"US": _brief("US", alerts=(regional_point,))}

    digest = build_global_situation_digest(
        regional_briefs,
        as_of="2026-07-11T09:00:00+00:00",
        global_brief=global_brief,
        max_points=3,
    )

    assert len(digest.points) == 3
    assert regional_point not in digest.points
    assert all(p in global_points for p in digest.points)


def test_without_global_brief_regional_behavior_is_byte_identical() -> None:
    """global_brief=None produces the exact same result as omitting the argument."""
    regional_briefs = {
        "US": _brief(
            "US",
            alerts=(_point("US alert.", severity="watch", signal="strong", source_ref="us"),),
        )
    }

    without = build_global_situation_digest(regional_briefs, as_of="2026-07-11T09:00:00+00:00")
    with_none = build_global_situation_digest(
        regional_briefs,
        as_of="2026-07-11T09:00:00+00:00",
        global_brief=None,
    )

    assert without == with_none


def test_global_digest_unknown_enums_and_round_trip_are_stable() -> None:
    digest = GlobalSituationDigest.from_mapping(
        {
            "as_of": "2026-07-11T09:00:00+00:00",
            "regime": "volatile",
            "rates_bias": "tight",
            "usd_bias": "up",
            "points": [
                {
                    "point": "Dollar weakening after a dovish cut.",
                    "sources": ["Reuters"],
                    "source_refs": [f"source-{index}" for index in range(10)],
                }
            ],
            "source_refs": [f"digest-{index}" for index in range(10)],
            "coverage": {"venues_seen": ["US", "US"], "point_count": 999},
        }
    )

    assert digest.regime == "unknown"
    assert digest.rates_bias == "unknown"
    assert digest.usd_bias == "unknown"
    assert len(digest.source_refs) == 8
    assert digest.coverage == {"venues_seen": ("US",), "point_count": 1}
    assert GlobalSituationDigest.from_mapping(digest.to_dict()) == digest
    assert GlobalSituationDigest.from_mapping({"as_of": digest.as_of}).regime == "unknown"
