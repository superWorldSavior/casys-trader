from trader.application.analyst import NewsMacroAnalysisRequest, run_news_macro_analysis
from trader.domain.situation import NewsMacroBrief


def _brief() -> NewsMacroBrief:
    brief = NewsMacroBrief.from_mapping(
        {
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

    def write(self, brief: NewsMacroBrief, *, date: str | None = None) -> None:
        self.writes.append((brief, date))


def test_run_news_macro_analysis_writes_brief() -> None:
    repository = FakeRepository()
    request = NewsMacroAnalysisRequest(
        as_of="2026-07-09T07:00:00+00:00",
        valid_until="2026-07-10T07:00:00+00:00",
        news_items=({"uuid": "u1", "title": "T"},),
    )

    result = run_news_macro_analysis(
        request,
        analyst=FakeAnalyst(),
        repository=repository,
        date="2026-07-09",
    )

    assert result.written is True
    assert result.brief_ref == {
        "date": "2026-07-09",
        "as_of": "2026-07-09T07:00:00+00:00",
    }
    assert repository.writes == [(_brief(), "2026-07-09")]


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
