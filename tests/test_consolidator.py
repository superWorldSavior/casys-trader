import json
from datetime import datetime, timezone

from trader import consolidator
from trader import llm
from trader.tools.memory import LearningsStore


def _raw(ts: str, symbol: str = "SPY", note: str = "range confirme") -> dict:
    return {"ts": ts, "symbol": symbol, "note": note, "action": "HOLD", "reason": "hold"}


def test_select_new_raw_filtre_par_watermark() -> None:
    rows = [
        _raw("2026-06-08T10:00:00+00:00", note="old"),
        _raw("2026-06-08T10:30:00+00:00", note="same"),
        _raw("2026-06-08T11:00:00+00:00", note="new"),
    ]

    selected = consolidator.select_new_raw(rows, watermark="2026-06-08T10:30:00+00:00")

    assert [item["note"] for item in selected] == ["new"]


def test_consolidated_store_valide_et_borne_la_sortie(tmp_path) -> None:
    store = consolidator.ConsolidatedLearningsStore(tmp_path / "learnings_consolidated.json")
    payload = {
        "global": [{"note": f"global {idx}", "robustness": "observed"} for idx in range(12)],
        "by_symbol": {
            "SPY": [{"note": f"spy {idx}"} for idx in range(6)],
            "": [{"note": "ignored"}],
        },
    }

    store.write(payload, watermark="2026-06-08T11:00:00+00:00")
    saved = store.read()

    assert saved["watermark"] == "2026-06-08T11:00:00+00:00"
    assert len(saved["global"]) == 10
    assert len(saved["by_symbol"]["SPY"]) == 5
    assert "" not in saved["by_symbol"]


def test_maybe_consolidate_attend_le_seuil(tmp_path) -> None:
    raw_store = LearningsStore(tmp_path / "learnings.jsonl", max_entries=200)
    consolidated_store = consolidator.ConsolidatedLearningsStore(tmp_path / "learnings_consolidated.json")
    now = datetime(2026, 6, 8, 10, tzinfo=timezone.utc)
    raw_store.append(symbol="SPY", note="un seul brut", now=now)

    result = consolidator.maybe_consolidate(raw_store, consolidated_store, threshold=2)

    assert result == {"triggered": False, "new_raw_count": 1}
    assert consolidated_store.read()["watermark"] is None


def test_maybe_consolidate_attend_50_bruts_par_defaut(tmp_path) -> None:
    raw_store = LearningsStore(tmp_path / "learnings.jsonl", max_entries=200)
    consolidated_store = consolidator.ConsolidatedLearningsStore(tmp_path / "learnings_consolidated.json")
    for idx in range(49):
        raw_store.append(
            symbol="SPY",
            note=f"brut {idx}",
            now=datetime(2026, 6, 8, 10, idx, tzinfo=timezone.utc),
        )

    result = consolidator.maybe_consolidate(raw_store, consolidated_store)

    assert result == {"triggered": False, "new_raw_count": 49}


def test_build_consolidator_router_depuis_env_dedie(monkeypatch) -> None:
    monkeypatch.setenv("TRADER_CONSOLIDATOR_ACPX_BIN", "acpx-review")
    monkeypatch.setenv("TRADER_CONSOLIDATOR_ACPX_AGENT", "codex")
    monkeypatch.setenv("TRADER_CONSOLIDATOR_MODEL", "gpt-5.5[high]")
    monkeypatch.delenv("TRADER_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    router = consolidator.build_consolidator_router_from_env(env_path=None)

    backend = router.backends[0]
    assert backend.provider == "consolidator"
    assert backend.acpx_bin == "acpx-review"
    assert backend.agent == "codex"
    assert backend.model == "gpt-5.5[high]"


def test_maybe_consolidate_ecrit_le_consolide_et_avance_le_watermark(tmp_path) -> None:
    raw_store = LearningsStore(tmp_path / "learnings.jsonl", max_entries=200)
    consolidated_store = consolidator.ConsolidatedLearningsStore(tmp_path / "learnings_consolidated.json")
    raw_store.append(symbol="SPY", note="z seul ne suffit pas", now=datetime(2026, 6, 8, 10, tzinfo=timezone.utc))
    raw_store.append(symbol="QQQ", note="attendre ER+AC", now=datetime(2026, 6, 8, 10, 30, tzinfo=timezone.utc))

    class Router:
        def complete(self, prompt: str, *, timeout_s: int):
            assert "z seul ne suffit pas" in prompt
            return llm.LlmCompletion(
                provider="test",
                model="stub",
                text=json.dumps(
                    {
                        "global": [{"note": "Ne pas trader z seul; exiger ER+AC.", "robustness": "observe sur indices"}],
                        "by_symbol": {"SPY": [{"note": "SPY reste range tant que ER bas."}]},
                    }
                ),
            )

    result = consolidator.maybe_consolidate(raw_store, consolidated_store, threshold=2, llm_router=Router())
    saved = consolidated_store.read()

    assert result == {"triggered": True, "new_raw_count": 2, "written": True}
    assert saved["watermark"] == "2026-06-08T10:30:00+00:00"
    assert saved["global"][0]["note"] == "Ne pas trader z seul; exiger ER+AC."
    assert saved["by_symbol"]["SPY"][0]["note"] == "SPY reste range tant que ER bas."


def test_maybe_consolidate_garde_letat_si_sortie_llm_invalide(tmp_path) -> None:
    raw_store = LearningsStore(tmp_path / "learnings.jsonl", max_entries=200)
    consolidated_store = consolidator.ConsolidatedLearningsStore(tmp_path / "learnings_consolidated.json")
    raw_store.append(symbol="SPY", note="brut", now=datetime(2026, 6, 8, 10, tzinfo=timezone.utc))

    class BadRouter:
        def complete(self, prompt: str, *, timeout_s: int):
            return llm.LlmCompletion(provider="test", model="stub", text="pas du json")

    result = consolidator.maybe_consolidate(raw_store, consolidated_store, threshold=1, llm_router=BadRouter())

    assert result == {
        "triggered": True,
        "new_raw_count": 1,
        "written": False,
        "error_code": "invalid_json",
        "error_message": "Expecting value: line 1 column 1 (char 0)",
    }
    assert consolidated_store.read()["watermark"] is None


def test_maybe_consolidate_ne_retente_pas_un_echec_sans_nouveau_lot(tmp_path) -> None:
    raw_store = LearningsStore(tmp_path / "learnings.jsonl", max_entries=200)
    consolidated_store = consolidator.ConsolidatedLearningsStore(tmp_path / "learnings_consolidated.json")
    status_store = consolidator.ConsolidationStatusStore(tmp_path / "learnings_consolidation_status.json")
    raw_store.append(symbol="SPY", note="brut 1", now=datetime(2026, 6, 8, 10, tzinfo=timezone.utc))
    raw_store.append(symbol="SPY", note="brut 2", now=datetime(2026, 6, 8, 10, 1, tzinfo=timezone.utc))
    calls = 0

    class BadRouter:
        def complete(self, prompt: str, *, timeout_s: int):
            nonlocal calls
            calls += 1
            return llm.LlmFailure(
                provider="consolidator",
                model="gpt-5.5[high]",
                code="nonzero_exit",
                message="adapter failed",
                retryable=False,
            )

    first = consolidator.maybe_consolidate(
        raw_store,
        consolidated_store,
        threshold=2,
        llm_router=BadRouter(),
        status_store=status_store,
    )
    second = consolidator.maybe_consolidate(
        raw_store,
        consolidated_store,
        threshold=2,
        llm_router=BadRouter(),
        status_store=status_store,
    )

    assert first == {
        "triggered": True,
        "new_raw_count": 2,
        "written": False,
        "error_code": "nonzero_exit",
        "error_message": "adapter failed",
    }
    assert second == {
        "triggered": False,
        "new_raw_count": 2,
        "skipped": True,
        "reason": "previous_failure_backoff",
        "new_raw_since_failure": 0,
        "retry_after_new_raw": 2,
        "last_error_code": "nonzero_exit",
    }
    assert calls == 1


def test_maybe_consolidate_retente_apres_un_nouveau_lot_depuis_lechec(tmp_path) -> None:
    raw_store = LearningsStore(tmp_path / "learnings.jsonl", max_entries=200)
    consolidated_store = consolidator.ConsolidatedLearningsStore(tmp_path / "learnings_consolidated.json")
    status_store = consolidator.ConsolidationStatusStore(tmp_path / "learnings_consolidation_status.json")
    raw_store.append(symbol="SPY", note="brut 1", now=datetime(2026, 6, 8, 10, tzinfo=timezone.utc))
    raw_store.append(symbol="SPY", note="brut 2", now=datetime(2026, 6, 8, 10, 1, tzinfo=timezone.utc))
    calls = 0

    class BadRouter:
        def complete(self, prompt: str, *, timeout_s: int):
            nonlocal calls
            calls += 1
            return llm.LlmCompletion(provider="test", model="stub", text="pas du json")

    first = consolidator.maybe_consolidate(
        raw_store,
        consolidated_store,
        threshold=2,
        llm_router=BadRouter(),
        status_store=status_store,
    )
    raw_store.append(symbol="QQQ", note="brut 3", now=datetime(2026, 6, 8, 10, 2, tzinfo=timezone.utc))
    skipped = consolidator.maybe_consolidate(
        raw_store,
        consolidated_store,
        threshold=2,
        llm_router=BadRouter(),
        status_store=status_store,
    )
    raw_store.append(symbol="QQQ", note="brut 4", now=datetime(2026, 6, 8, 10, 3, tzinfo=timezone.utc))
    retried = consolidator.maybe_consolidate(
        raw_store,
        consolidated_store,
        threshold=2,
        llm_router=BadRouter(),
        status_store=status_store,
    )

    assert first["written"] is False
    assert skipped["skipped"] is True
    assert skipped["new_raw_since_failure"] == 1
    assert retried["triggered"] is True
    assert retried["written"] is False
    assert calls == 2


def test_maybe_consolidate_nettoie_le_status_apres_succes_suivant_un_echec(tmp_path) -> None:
    raw_store = LearningsStore(tmp_path / "learnings.jsonl", max_entries=200)
    consolidated_store = consolidator.ConsolidatedLearningsStore(tmp_path / "learnings_consolidated.json")
    status_store = consolidator.ConsolidationStatusStore(tmp_path / "learnings_consolidation_status.json")
    raw_store.append(symbol="SPY", note="brut 1", now=datetime(2026, 6, 8, 10, tzinfo=timezone.utc))
    raw_store.append(symbol="SPY", note="brut 2", now=datetime(2026, 6, 8, 10, 1, tzinfo=timezone.utc))
    calls = 0

    class BadRouter:
        def complete(self, prompt: str, *, timeout_s: int):
            nonlocal calls
            calls += 1
            if calls == 1:
                return llm.LlmCompletion(provider="test", model="stub", text="pas du json")
            return llm.LlmCompletion(
                provider="test",
                model="stub",
                text=json.dumps(
                    {
                        "global": [{"note": "Attendre confirmation avant de trader.", "robustness": "observe"}],
                        "by_symbol": {"SPY": [{"note": "SPY range tant que volume absent."}]},
                    }
                ),
            )

    first = consolidator.maybe_consolidate(
        raw_store,
        consolidated_store,
        threshold=2,
        llm_router=BadRouter(),
        status_store=status_store,
    )
    raw_store.append(symbol="QQQ", note="brut 3", now=datetime(2026, 6, 8, 10, 2, tzinfo=timezone.utc))
    raw_store.append(symbol="QQQ", note="brut 4", now=datetime(2026, 6, 8, 10, 3, tzinfo=timezone.utc))
    second = consolidator.maybe_consolidate(
        raw_store,
        consolidated_store,
        threshold=2,
        llm_router=BadRouter(),
        status_store=status_store,
    )

    assert first["written"] is False
    assert first["error_code"] == "invalid_json"
    assert second["written"] is True
    assert calls == 2
    assert status_store.path.exists() is False


def test_maybe_consolidate_ignore_le_backoff_si_watermark_consolide_avance(tmp_path) -> None:
    raw_store = LearningsStore(tmp_path / "learnings.jsonl", max_entries=200)
    consolidated_store = consolidator.ConsolidatedLearningsStore(tmp_path / "learnings_consolidated.json")
    status_store = consolidator.ConsolidationStatusStore(tmp_path / "learnings_consolidation_status.json")
    w0 = "2026-06-08T09:00:00+00:00"
    w1 = "2026-06-08T09:30:00+00:00"
    consolidated_store.write({"global": [{"note": "ancienne synthese"}], "by_symbol": {}}, watermark=w0)
    raw_store.append(symbol="SPY", note="brut 1", now=datetime(2026, 6, 8, 10, tzinfo=timezone.utc))
    raw_store.append(symbol="SPY", note="brut 2", now=datetime(2026, 6, 8, 10, 1, tzinfo=timezone.utc))
    calls = 0

    class BadRouter:
        def complete(self, prompt: str, *, timeout_s: int):
            nonlocal calls
            calls += 1
            return llm.LlmCompletion(provider="test", model="stub", text="pas du json")

    first = consolidator.maybe_consolidate(
        raw_store,
        consolidated_store,
        threshold=2,
        llm_router=BadRouter(),
        status_store=status_store,
    )
    last_failure = status_store.read()["last_failure"]
    consolidated_store.write({"global": [{"note": "synthese externe"}], "by_symbol": {}}, watermark=w1)
    second = consolidator.maybe_consolidate(
        raw_store,
        consolidated_store,
        threshold=2,
        llm_router=BadRouter(),
        status_store=status_store,
    )

    assert first["written"] is False
    assert last_failure["consolidated_watermark"] == w0
    assert calls == 2
    assert second["triggered"] is True
    assert second["written"] is False
    assert second.get("skipped") is not True
    assert second.get("reason") != "previous_failure_backoff"


def test_build_context_learnings_preserve_le_fallback_froid() -> None:
    raw_recent = [_raw("2026-06-08T10:00:00+00:00", note="cassure ratee")]
    empty = consolidator.empty_consolidated()

    assert consolidator.build_context_learnings(empty, raw_recent=raw_recent) == raw_recent


def test_build_context_learnings_injecte_consolide_et_bruts_recents() -> None:
    raw_recent = [
        _raw("2026-06-08T10:00:00+00:00", symbol="SPY", note="raw 1"),
        _raw("2026-06-08T10:30:00+00:00", symbol="QQQ", note="raw 2"),
    ]
    consolidated = {
        "watermark": "2026-06-08T10:30:00+00:00",
        "global": [{"note": "z seul ne suffit pas"}],
        "by_symbol": {"SPY": [{"note": "SPY range"}]},
    }

    context = consolidator.build_context_learnings(consolidated, raw_recent=raw_recent)

    assert context == {
        "global": [{"note": "z seul ne suffit pas"}],
        "by_symbol": {"SPY": [{"note": "SPY range"}]},
        "raw_recent": raw_recent,
    }
