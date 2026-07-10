from trader.application.analyst import NewsMacroAnalysisRequest, run_news_macro_analysis
from trader.domain.situation import NewsMacroBrief


def _brief() -> NewsMacroBrief:
    brief = NewsMacroBrief.from_mapping(
        {
            "venue": "EU",
            "as_of": "2026-07-09T07:00:00+00:00",
            "valid_until": "2026-07-10T07:00:00+00:00",
            "alerts": [{"point": "Risk-off broadening", "severity": "watch"}],
        }
    )
    assert brief is not None
    return brief


class FakeAnalyst:
    def analyze(self, request: NewsMacroAnalysisRequest) -> NewsMacroBrief:
        assert request.news_items == ({"uuid": "u1", "title": "T"},)
        return _brief()


class FakeRepository:
    def __init__(self) -> None:
        self.writes: list[tuple[NewsMacroBrief, str | None]] = []

    def append(self, brief: NewsMacroBrief, *, date: str | None = None) -> dict[str, str]:
        self.writes.append((brief, date))
        return brief.ref(date=date or brief.as_of[:10])


class FakeSituationRepository:
    def __init__(self) -> None:
        self.ingested: list[NewsMacroBrief] = []

    def ingest_brief(self, brief: NewsMacroBrief) -> dict:
        self.ingested.append(brief)
        return {"inserted": 1, "skipped": 0}


def test_run_news_macro_analysis_writes_brief() -> None:
    repository = FakeRepository()
    situation_repository = FakeSituationRepository()
    request = NewsMacroAnalysisRequest(
        as_of="2026-07-09T07:00:00+00:00",
        valid_until="2026-07-10T07:00:00+00:00",
        venue="EU",
        news_items=({"uuid": "u1", "title": "T"},),
    )

    result = run_news_macro_analysis(
        request,
        analyst=FakeAnalyst(),
        repository=repository,
        situation_repository=situation_repository,
        date="2026-07-09",
    )

    assert result.written is True
    assert result.brief_ref == {
        "date": "2026-07-09",
        "venue": "EU",
        "brief_id": "2026-07-09T07:00:00+00:00|EU",
        "as_of": "2026-07-09T07:00:00+00:00",
    }
    assert repository.writes == [(_brief(), "2026-07-09")]
    assert situation_repository.ingested == [_brief()]


def test_run_news_macro_analysis_is_best_effort_on_failure() -> None:
    class BrokenAnalyst:
        def analyze(self, request: NewsMacroAnalysisRequest) -> NewsMacroBrief:
            raise RuntimeError("llm down")

    result = run_news_macro_analysis(
        NewsMacroAnalysisRequest(as_of="a", valid_until="b"),
        analyst=BrokenAnalyst(),
        repository=FakeRepository(),
    )

    assert result.written is False
    assert result.error_code == "RuntimeError"
    assert result.error_message == "llm down"


def test_run_news_macro_analysis_keeps_written_when_derived_index_fails() -> None:
    class BrokenSituationRepository:
        def ingest_brief(self, brief: NewsMacroBrief) -> dict:
            raise RuntimeError("sqlite locked")

    repository = FakeRepository()
    result = run_news_macro_analysis(
        NewsMacroAnalysisRequest(
            as_of="2026-07-09T07:00:00+00:00",
            valid_until="2026-07-10T07:00:00+00:00",
            venue="EU",
            news_items=({"uuid": "u1", "title": "T"},),
        ),
        analyst=FakeAnalyst(),
        repository=repository,
        situation_repository=BrokenSituationRepository(),
    )

    assert result.written is True
    assert repository.writes == [(_brief(), None)]
