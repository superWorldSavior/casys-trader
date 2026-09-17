from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from tests.package_layout._helpers import REPO_ROOT, _domain_import_violations
from trader.domain.situation.brief import SituationPoint
from trader.domain.world_news import (
    DRIVER_NEWS_BUNDLE_SCHEMA,
    NEWS_ATTRIBUTION_QUALITY,
    NEWS_DIRECTIONS,
    NEWS_EVENT_CLASSES,
    NEWS_HORIZON_BUCKETS,
    DriverNewsBundle,
    bucket_news_horizon,
    classify_news_event_class,
    news_signal_from_point,
)

MODULE_PATH = REPO_ROOT / "trader" / "domain" / "world_news.py"


def _point(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "point": "Board declares a quarterly dividend of $0.50 per share.",
        "symbols": ("TTE.PA",),
        "source_refs": ("yahoo:uuid-1",),
        "severity": "info",
        "signal": "weak",
        "event_class": "capital",
        "horizon": "next month",
        "direction": "bullish",
    }
    values.update(overrides)
    return values


def _bundle(**overrides: object) -> DriverNewsBundle:
    values: dict[str, object] = {
        "event_class": "capital",
        "event_class_source": "analyst",
        "direction": "bullish",
        "strength": "weak",
        "severity": "info",
        "horizon_bucket": "months",
        "attribution_quality": "symbol_sourced",
    }
    values.update(overrides)
    return DriverNewsBundle(**values)  # type: ignore[arg-type]


def test_event_class_rules_are_deterministic_and_ordered() -> None:
    assert classify_news_event_class("Q3 earnings beat estimates with EPS of $2.10") == "earnings"
    assert classify_news_event_class("Company raises full-year guidance and outlook") == "guidance"
    assert classify_news_event_class("Board approves merger with rival in $4B takeover") == "corporate_action"
    assert classify_news_event_class("Regulator opens antitrust investigation, SEC fines loom") == "regulatory"
    assert classify_news_event_class("Dividend raised alongside a $1B buyback") == "capital"
    assert classify_news_event_class("CEO resigns; strike shuts down the main plant") == "operations"
    assert classify_news_event_class("Fed rate cut bets rise as CPI inflation cools") == "macro"
    # First match in class order wins, not first keyword in text.
    assert classify_news_event_class("Inflation fears fade as earnings beat estimates") == "earnings"
    assert classify_news_event_class("No fresh earnings news for this name") == "no_news"
    assert classify_news_event_class("Aucune news fraîche sur le dossier") == "no_news"
    assert classify_news_event_class("STLA sans catalyseur news") == "no_news"
    assert classify_news_event_class("Broker upgrades to overweight, price target raised") == "analyst"
    assert classify_news_event_class("Recommandation relevée, objectif de cours haussier") == "analyst"
    assert classify_news_event_class("Résultats supérieurs aux attentes au T3") == "earnings"
    assert classify_news_event_class("Fusion annoncée et dividende relevé") == "corporate_action"
    assert classify_news_event_class("Record orders and A330 deliveries support momentum") == "operations"
    assert classify_news_event_class("ECB deposit rate before FOMC; taux sous pression") == "macro"
    assert classify_news_event_class("") == "unknown"
    assert classify_news_event_class(None) == "unknown"
    assert classify_news_event_class(42) == "unknown"
    assert classify_news_event_class("  ") == "unknown"


def test_horizon_buckets_prefer_forward_markers_over_reported() -> None:
    assert bucket_news_horizon(None) == "unspecified"
    assert bucket_news_horizon("") == "unspecified"
    assert bucket_news_horizon("already reported; H2 2026 follow-through") == "quarters"
    assert bucket_news_horizon("next 12-24 months") == "years"
    assert bucket_news_horizon("Q4 2026") == "quarters"
    assert bucket_news_horizon("coming weeks") == "weeks"
    assert bucket_news_horizon("tomorrow morning") == "days"
    assert bucket_news_horizon("already reported") == "reported"
    assert bucket_news_horizon("some vague soon-ish wording") == "unspecified"
    assert bucket_news_horizon(42) == "unspecified"


def test_news_signal_from_point_copies_closed_semantics() -> None:
    bundle = news_signal_from_point(_point())
    assert bundle == _bundle()
    assert bundle.schema_version == DRIVER_NEWS_BUNDLE_SCHEMA
    assert bundle.identity_tuple() == (
        "capital",
        "analyst",
        "bullish",
        "weak",
        "info",
        "months",
        "symbol_sourced",
    )
    assert bundle.event_class in NEWS_EVENT_CLASSES
    assert bundle.direction in NEWS_DIRECTIONS
    assert bundle.horizon_bucket in NEWS_HORIZON_BUCKETS
    assert bundle.attribution_quality in NEWS_ATTRIBUTION_QUALITY


def test_news_signal_reads_stored_class_and_rejects_missing() -> None:
    # Stored class wins over text: no keyword re-derivation.
    bundle = news_signal_from_point(_point(event_class="regulatory"))
    assert bundle.event_class == "regulatory"
    assert bundle.event_class_source == "analyst"
    with pytest.raises(ValueError, match="no analyst event_class"):
        news_signal_from_point(_point(event_class=None))
    with pytest.raises(ValueError, match="event_class must be one of"):
        news_signal_from_point(_point(event_class="vibes"))


def test_news_bundle_migrates_v1_with_keywords_source() -> None:
    from trader.domain.world_news import DRIVER_NEWS_BUNDLE_SCHEMA_V1

    legacy = {
        "schema_version": DRIVER_NEWS_BUNDLE_SCHEMA_V1,
        "event_class": "earnings",
        "direction": "bullish",
        "strength": "strong",
        "severity": "watch",
        "horizon_bucket": "quarters",
        "attribution_quality": "symbol_sourced",
    }
    migrated = DriverNewsBundle.from_mapping(legacy)
    assert migrated.event_class_source == "keywords"
    assert migrated.schema_version == DRIVER_NEWS_BUNDLE_SCHEMA_V1
    with pytest.raises(ValueError, match="forbids event_class_source"):
        DriverNewsBundle.from_mapping({**legacy, "event_class_source": "analyst"})
    with pytest.raises(ValueError, match="schema_version"):
        DriverNewsBundle.from_mapping({**_bundle().to_dict(), "schema_version": "driver_news_bundle.v9"})
    with pytest.raises(TypeError, match="non-empty string"):
        DriverNewsBundle.from_mapping(
            {key: value for key, value in _bundle().to_dict().items() if key != "event_class_source"}
        )


def test_news_signal_accepts_situation_point_and_defaults_direction() -> None:
    parsed = SituationPoint.from_mapping(_point(direction=None))
    assert parsed is not None
    assert news_signal_from_point(parsed).direction == "unspecified"


def test_news_signal_grades_attribution_quality() -> None:
    assert news_signal_from_point(_point()).attribution_quality == "symbol_sourced"
    assert news_signal_from_point(_point(source_refs=())).attribution_quality == "symbol_only"
    assert news_signal_from_point(_point(symbols=(), source_refs=())).attribution_quality == "unattributed"


def test_news_signal_rejects_operational_noise_and_empty_text() -> None:
    with pytest.raises(ValueError, match="operational"):
        news_signal_from_point(_point(is_operational=True))
    with pytest.raises(ValueError, match="no usable text"):
        news_signal_from_point(_point(point="   "))
    with pytest.raises(TypeError, match="SituationPoint or a mapping"):
        news_signal_from_point(42)  # type: ignore[arg-type]


def test_news_signal_coerces_invalid_enums_through_brief_contract() -> None:
    bundle = news_signal_from_point(_point(severity="critical", signal="loud", direction="moon"))
    assert (bundle.severity, bundle.strength, bundle.direction) == ("info", "weak", "unspecified")


def test_news_bundle_rejects_open_and_raw_fields() -> None:
    bundle = _bundle()
    replayed = DriverNewsBundle.from_mapping(bundle.to_dict())
    assert replayed == bundle
    with pytest.raises(ValueError, match="event_class"):
        _bundle(event_class="vibes")
    with pytest.raises(ValueError, match="horizon_bucket"):
        _bundle(horizon_bucket="fortnight")
    with pytest.raises(ValueError, match="schema_version"):
        DriverNewsBundle.from_mapping({**bundle.to_dict(), "schema_version": "driver_news_bundle.v9"})
    for raw in ("point", "title", "uuid", "link", "symbol", "brief_id"):
        with pytest.raises(ValueError, match="raw fields"):
            DriverNewsBundle.from_mapping({**bundle.to_dict(), raw: "leak"})
    with pytest.raises(ValueError, match="unknown fields"):
        DriverNewsBundle.from_mapping({**bundle.to_dict(), "provider": "yahoo"})
    with pytest.raises(FrozenInstanceError):
        bundle.event_class = "earnings"  # type: ignore[misc]


def test_world_news_stays_stdlib_domain() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "trader.application" not in source
    assert "trader.infrastructure" not in source
    assert "trader.runtime" not in source
    assert _domain_import_violations([MODULE_PATH], REPO_ROOT) == []
