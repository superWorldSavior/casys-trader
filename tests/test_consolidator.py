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
    monkeypatch.setenv("TRADER_CONSOLIDATOR_MODEL", "gpt-5.5/high")
    monkeypatch.delenv("TRADER_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    router = consolidator.build_consolidator_router_from_env(env_path=None)

    backend = router.backends[0]
    assert backend.provider == "consolidator"
    assert backend.acpx_bin == "acpx-review"
    assert backend.agent == "codex"
    assert backend.model == "gpt-5.5/high"


def test_build_consolidator_router_modele_defaut_acpx_annonce(monkeypatch) -> None:
    monkeypatch.delenv("TRADER_CONSOLIDATOR_MODEL", raising=False)
    monkeypatch.delenv("TRADER_CONSOLIDATOR_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("TRADER_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    router = consolidator.build_consolidator_router_from_env(env_path=None)

    backend = router.backends[0]
    assert backend.provider == "consolidator"
    assert backend.model == "gpt-5.5"


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


def test_maybe_consolidate_parse_le_json_final_apres_messages_acpx(tmp_path) -> None:
    raw_store = LearningsStore(tmp_path / "learnings.jsonl", max_entries=200)
    consolidated_store = consolidator.ConsolidatedLearningsStore(tmp_path / "learnings_consolidated.json")
    raw_store.append(symbol="SPY", note="brut", now=datetime(2026, 6, 8, 10, tzinfo=timezone.utc))

    class ChattyRouter:
        def complete(self, prompt: str, *, timeout_s: int):
            payload = json.dumps(
                {
                    "global": [{"note": "Agir seulement sur cassure confirmee."}],
                    "by_symbol": {"SPY": [{"note": "SPY attend le breakout."}]},
                }
            )
            return llm.LlmCompletion(
                provider="consolidator",
                model="gpt-5.5",
                text=(
                    "J'utilise le workflow de consolidation avant le JSON.\n"
                    "Le transport acpx peut concatener plusieurs messages.\n"
                    f"{payload}\n"
                ),
            )

    result = consolidator.maybe_consolidate(raw_store, consolidated_store, threshold=1, llm_router=ChattyRouter())
    saved = consolidated_store.read()

    assert result == {"triggered": True, "new_raw_count": 1, "written": True}
    assert saved["global"] == [{"note": "Agir seulement sur cassure confirmee."}]
    assert saved["by_symbol"] == {"SPY": [{"note": "SPY attend le breakout."}]}


def test_maybe_consolidate_repare_un_json_final_tronque_en_fin_de_stdout(tmp_path) -> None:
    raw_store = LearningsStore(tmp_path / "learnings.jsonl", max_entries=200)
    consolidated_store = consolidator.ConsolidatedLearningsStore(tmp_path / "learnings_consolidated.json")
    raw_store.append(symbol="SPY", note="brut", now=datetime(2026, 6, 8, 10, tzinfo=timezone.utc))
    valid_payload = json.dumps(
        {
            "global": [{"note": "Tenir les gagnants structurels."}],
            "by_symbol": {"SPY": [{"note": "SPY sort seulement sur breakdown."}]},
        }
    )
    truncated_payload = valid_payload[:-1]

    class TruncatedRouter:
        def complete(self, prompt: str, *, timeout_s: int):
            return llm.LlmCompletion(provider="consolidator", model="gpt-5.5", text=truncated_payload)

    result = consolidator.maybe_consolidate(
        raw_store,
        consolidated_store,
        threshold=1,
        llm_router=TruncatedRouter(),
    )
    saved = consolidated_store.read()

    assert result == {"triggered": True, "new_raw_count": 1, "written": True}
    assert saved["global"] == [{"note": "Tenir les gagnants structurels."}]
    assert saved["by_symbol"] == {"SPY": [{"note": "SPY sort seulement sur breakdown."}]}


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
        "provider": "test",
        "model": "stub",
        "output_preview": "pas du json",
        "output_tail": "pas du json",
        "output_length": 11,
    }
    assert consolidated_store.read()["watermark"] is None


def test_maybe_consolidate_status_observe_la_sortie_non_json(tmp_path) -> None:
    raw_store = LearningsStore(tmp_path / "learnings.jsonl", max_entries=200)
    consolidated_store = consolidator.ConsolidatedLearningsStore(tmp_path / "learnings_consolidated.json")
    status_store = consolidator.ConsolidationStatusStore(tmp_path / "learnings_consolidation_status.json")
    raw_store.append(symbol="SPY", note="brut", now=datetime(2026, 6, 8, 10, tzinfo=timezone.utc))

    class BadRouter:
        def complete(self, prompt: str, *, timeout_s: int):
            return llm.LlmCompletion(provider="consolidator", model="gpt-5.5", text="pas du json")

    consolidator.maybe_consolidate(
        raw_store,
        consolidated_store,
        threshold=1,
        llm_router=BadRouter(),
        status_store=status_store,
    )

    last_failure = status_store.read()["last_failure"]
    assert last_failure["provider"] == "consolidator"
    assert last_failure["model"] == "gpt-5.5"
    assert last_failure["output_preview"] == "pas du json"
    assert last_failure["output_tail"] == "pas du json"
    assert last_failure["output_length"] == 11


def test_build_consolidation_prompt_interdit_les_messages_hors_json() -> None:
    prompt = consolidator.build_consolidation_prompt(consolidator.empty_consolidated(), [])

    assert "Ne produis aucun message de statut" in prompt
    assert "un seul message assistant" in prompt
    assert "JSON pur" in prompt


def test_maybe_consolidate_retente_un_echec_retryable_puis_ecrit_le_consolide(tmp_path) -> None:
    raw_store = LearningsStore(tmp_path / "learnings.jsonl", max_entries=200)
    consolidated_store = consolidator.ConsolidatedLearningsStore(tmp_path / "learnings_consolidated.json")
    raw_store.append(symbol="SPY", note="brut retryable", now=datetime(2026, 6, 8, 10, tzinfo=timezone.utc))
    calls = 0

    class Router:
        def complete(self, prompt: str, *, timeout_s: int):
            nonlocal calls
            calls += 1
            if calls < 3:
                return llm.LlmFailure(
                    provider="consolidator",
                    model="gpt-5.5/high",
                    code="internal_error",
                    message="Internal error",
                    retryable=True,
                )
            return llm.LlmCompletion(
                provider="test",
                model="stub",
                text=json.dumps({"global": [{"note": "agir sur cassure confirmee"}], "by_symbol": {}}),
            )

    result = consolidator.maybe_consolidate(
        raw_store,
        consolidated_store,
        threshold=1,
        llm_router=Router(),
        max_attempts=3,
    )

    assert result == {"triggered": True, "new_raw_count": 1, "written": True}
    assert calls == 3
    assert consolidated_store.read()["global"] == [{"note": "agir sur cassure confirmee"}]


def test_maybe_consolidate_enregistre_un_seul_echec_apres_retries_epuises(tmp_path) -> None:
    raw_store = LearningsStore(tmp_path / "learnings.jsonl", max_entries=200)
    consolidated_store = consolidator.ConsolidatedLearningsStore(tmp_path / "learnings_consolidated.json")
    raw_store.append(symbol="SPY", note="brut retryable", now=datetime(2026, 6, 8, 10, tzinfo=timezone.utc))
    calls = 0

    class CountingStatusStore(consolidator.ConsolidationStatusStore):
        writes = 0

        def write_failure(self, **kwargs):
            self.writes += 1
            return super().write_failure(**kwargs)

    class Router:
        def complete(self, prompt: str, *, timeout_s: int):
            nonlocal calls
            calls += 1
            return llm.LlmFailure(
                provider="consolidator",
                model="gpt-5.5/high",
                code="internal_error",
                message="Internal error",
                retryable=True,
            )

    status_store = CountingStatusStore(tmp_path / "learnings_consolidation_status.json")
    result = consolidator.maybe_consolidate(
        raw_store,
        consolidated_store,
        threshold=1,
        llm_router=Router(),
        status_store=status_store,
        max_attempts=3,
    )

    assert result["written"] is False
    assert result["error_code"] == "internal_error"
    assert calls == 3
    assert status_store.writes == 1
    assert status_store.read()["last_failure"]["error_code"] == "internal_error"


def test_maybe_consolidate_ne_retente_pas_un_echec_non_retryable(tmp_path) -> None:
    raw_store = LearningsStore(tmp_path / "learnings.jsonl", max_entries=200)
    consolidated_store = consolidator.ConsolidatedLearningsStore(tmp_path / "learnings_consolidated.json")
    raw_store.append(symbol="SPY", note="brut fatal", now=datetime(2026, 6, 8, 10, tzinfo=timezone.utc))
    calls = 0

    class CountingStatusStore(consolidator.ConsolidationStatusStore):
        writes = 0

        def write_failure(self, **kwargs):
            self.writes += 1
            return super().write_failure(**kwargs)

    class Router:
        def complete(self, prompt: str, *, timeout_s: int):
            nonlocal calls
            calls += 1
            return llm.LlmFailure(
                provider="consolidator",
                model="gpt-5.5",
                code="nonzero_exit",
                message="adapter failed",
                retryable=False,
            )

    status_store = CountingStatusStore(tmp_path / "learnings_consolidation_status.json")
    result = consolidator.maybe_consolidate(
        raw_store,
        consolidated_store,
        threshold=1,
        llm_router=Router(),
        status_store=status_store,
        max_attempts=3,
    )

    assert result["written"] is False
    assert result["error_code"] == "nonzero_exit"
    assert calls == 1
    assert status_store.writes == 1


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
                model="gpt-5.5",
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


def test_maybe_consolidate_retente_si_le_modele_a_change_depuis_lechec(tmp_path) -> None:
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
                model="gpt-5.5[high]" if calls == 1 else "gpt-5.5/high",
                code="nonzero_exit",
                message="adapter failed",
                retryable=False,
            )

    first = consolidator.maybe_consolidate(
        raw_store,
        consolidated_store,
        threshold=2,
        llm_router=BadRouter(),
        model="gpt-5.5[high]",
        status_store=status_store,
    )
    retried = consolidator.maybe_consolidate(
        raw_store,
        consolidated_store,
        threshold=2,
        llm_router=BadRouter(),
        model="gpt-5.5/high",
        status_store=status_store,
    )

    assert first["written"] is False
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


def test_build_context_learnings_injecte_le_consolide_seul() -> None:
    # D6 : les bruts ne sont plus réinjectés dès qu'un consolidé existe
    # (ils répétaient les derniers HOLD et nourrissaient l'auto-renforcement).
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
    }


# --- D6 (registre) : casser la boucle d'auto-renforcement HOLD ---


def test_prompt_consolidation_priorise_les_entrees_et_cape_les_abstentions() -> None:
    prompt = consolidator.build_consolidation_prompt(consolidator.empty_consolidated(), [])

    # priorité explicite aux patterns d'entrée (sous-représentés dans les bruts)
    assert "entrée" in prompt.lower()
    # cap explicite sur les règles d'abstention
    assert "abstention" in prompt.lower()
    # l'ancienne consigne qui faisait gagner la règle majoritaire (HOLD) disparaît
    assert "préserve les entrées stables" not in prompt


def test_prompt_consolidation_injecte_lattribution_et_les_consigne_qualite() -> None:
    attribution = {
        "n_closed_trades": 5,
        "realized_pnl": -12.5,
        "realized_gross_pnl": 4.0,
        "total_commissions": 16.5,
        "win_rate": 0.4,
        "by_exit_reason": [
            {"reason": "trailing_stop", "n": 3, "total_pnl": -18.0, "total_commission": 6.0}
        ],
        "by_confidence": [{"bucket": "high", "n": 3, "total_pnl": 22.0}],
        "regime": {"since": "2026-06-10", "excluded_symbols": ["CL=F"]},
    }

    prompt = consolidator.build_consolidation_prompt(
        consolidator.empty_consolidated(),
        [_raw("2026-06-10T10:00:00+00:00", note="HOLD range")],
        attribution=attribution,
    )
    payload = json.loads(prompt.rsplit("\n\n", 1)[1])

    assert payload["attribution"] == attribution
    assert "robustness" in prompt
    assert "3 occurrences" in prompt
    assert "P&L cohérent" in prompt
    assert "AU MOINS 3 règles `global`" in prompt
    assert "conditions d'ACTION positives" in prompt
    assert "AU PLUS 4 règles d'abstention" in prompt
    assert "GESTION DE SORTIE" in prompt
    assert "trailing_stop" in prompt
    assert "total_commissions" in prompt
    assert "by_symbol" in prompt
    assert "SPÉCIFIQUE au symbole" in prompt
    assert "Interdit de reformuler une règle globale par symbole" in prompt


def test_prompt_consolidation_injecte_les_stats_meta_descriptives() -> None:
    meta = {
        "available": True,
        "horizons": {
            "4h": {
                "global": {"known": 100, "missed_known_pct": 56.0},
                "hold_quality_by_reason": [
                    {"reason_code": "WAITING_PULLBACK", "known": 10, "missed_known_pct": 80.0}
                ],
                "trade_quality_by_reason": [
                    {"action": "BUY", "reason_code": "ENTRY_SIGNAL", "known": 5, "bad_known_pct": 40.0}
                ],
            }
        },
    }

    prompt = consolidator.build_consolidation_prompt(
        consolidator.empty_consolidated(),
        [_raw("2026-06-10T10:00:00+00:00", note="attendre pullback")],
        meta_performance=meta,
    )
    payload = json.loads(prompt.rsplit("\n\n", 1)[1])

    assert payload["meta_performance"] == meta
    assert "stats META" in prompt
    assert "HOLD missed" in prompt
    assert "reason_code" in prompt


def test_maybe_consolidate_transmet_lattribution_au_prompt_llm(tmp_path) -> None:
    raw_store = LearningsStore(tmp_path / "learnings.jsonl", max_entries=200)
    consolidated_store = consolidator.ConsolidatedLearningsStore(tmp_path / "learnings_consolidated.json")
    raw_store.append(symbol="SPY", note="brut avec attribution", now=datetime(2026, 6, 8, 10, tzinfo=timezone.utc))
    attribution = {
        "n_closed_trades": 1,
        "realized_pnl": 42.0,
        "realized_gross_pnl": 45.0,
        "total_commissions": 3.0,
        "win_rate": 1.0,
        "by_exit_reason": [{"reason": "take_profit", "n": 1, "total_pnl": 42.0, "total_commission": 3.0}],
        "by_confidence": [{"bucket": "high", "n": 1, "total_pnl": 42.0}],
        "regime": {"since": "2026-06-01", "excluded_symbols": []},
    }
    prompts: list[str] = []

    class Router:
        def complete(self, prompt: str, *, timeout_s: int):
            prompts.append(prompt)
            return llm.LlmCompletion(
                provider="test",
                model="stub",
                text=json.dumps({"global": [{"note": "agir quand la cassure confirme"}], "by_symbol": {}}),
            )

    result = consolidator.maybe_consolidate(
        raw_store,
        consolidated_store,
        threshold=1,
        llm_router=Router(),
        attribution=attribution,
    )
    payload = json.loads(prompts[0].rsplit("\n\n", 1)[1])

    assert result["written"] is True
    assert payload["attribution"] == attribution


def test_maybe_consolidate_transmet_meta_performance_au_prompt_llm(tmp_path) -> None:
    raw_store = LearningsStore(tmp_path / "learnings.jsonl", max_entries=200)
    consolidated_store = consolidator.ConsolidatedLearningsStore(tmp_path / "learnings_consolidated.json")
    raw_store.append(symbol="SPY", note="brut avec meta", now=datetime(2026, 6, 8, 10, tzinfo=timezone.utc))
    meta = {"available": True, "horizons": {"1h": {"global": {"missed_known_pct": 50.0}}}}
    prompts: list[str] = []

    class Router:
        def complete(self, prompt: str, *, timeout_s: int):
            prompts.append(prompt)
            return llm.LlmCompletion(
                provider="test",
                model="stub",
                text=json.dumps({"global": [{"note": "meta descriptif"}], "by_symbol": {}}),
            )

    result = consolidator.maybe_consolidate(
        raw_store,
        consolidated_store,
        threshold=1,
        llm_router=Router(),
        meta_performance=meta,
    )
    payload = json.loads(prompts[0].rsplit("\n\n", 1)[1])

    assert result["written"] is True
    assert payload["meta_performance"] == meta


def test_main_run_declenche_la_consolidation_sur_un_state_tmp(monkeypatch, tmp_path, capsys) -> None:
    state_dir = tmp_path / "state"
    raw_store = LearningsStore(state_dir / "learnings.jsonl", max_entries=200)
    raw_store.append(symbol="SPY", note="brut 1", now=datetime(2026, 6, 8, 10, tzinfo=timezone.utc))
    raw_store.append(symbol="QQQ", note="brut 2", now=datetime(2026, 6, 8, 10, 1, tzinfo=timezone.utc))

    class Router:
        def complete(self, prompt: str, *, timeout_s: int):
            assert '"attribution"' in prompt
            return llm.LlmCompletion(
                provider="test",
                model="stub",
                text=json.dumps({"global": [{"note": "agir quand le signal confirme"}], "by_symbol": {}}),
            )

    monkeypatch.setattr(
        consolidator,
        "build_consolidator_router_from_env",
        lambda **kwargs: Router(),
    )

    exit_code = consolidator.main(["--run", "--state-dir", str(state_dir), "--threshold", "2"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output == {"triggered": True, "new_raw_count": 2, "written": True}
    saved = consolidator.ConsolidatedLearningsStore(state_dir / "learnings_consolidated.json").read()
    assert saved["global"] == [{"note": "agir quand le signal confirme"}]


def test_build_context_learnings_sans_bruts_quand_consolide_existe() -> None:
    consolidated = {
        "watermark": "2026-06-08T10:30:00+00:00",
        "global": [{"note": "z seul ne suffit pas"}],
        "by_symbol": {},
    }

    context = consolidator.build_context_learnings(
        consolidated,
        raw_recent=[_raw("2026-06-08T10:00:00+00:00")],
    )

    assert "raw_recent" not in context


def test_build_context_learnings_injecte_les_guardrails_separes() -> None:
    guardrails = [{"note": "Toute ouverture s'accompagne d'un exit_plan avec stop."}]
    consolidated = {
        "watermark": "2026-06-08T10:30:00+00:00",
        "global": [{"note": "pattern machine, remettable en question"}],
        "by_symbol": {},
    }

    context = consolidator.build_context_learnings(
        consolidated, raw_recent=[], guardrails=guardrails
    )
    assert context["guardrails"] == guardrails

    # cold-start : les guardrails restent présents même sans consolidé
    cold = consolidator.build_context_learnings(
        consolidator.empty_consolidated(),
        raw_recent=[_raw("2026-06-08T10:00:00+00:00")],
        guardrails=guardrails,
    )
    assert cold["guardrails"] == guardrails
    assert cold["raw_recent"] == [_raw("2026-06-08T10:00:00+00:00")]


def test_load_guardrails(tmp_path) -> None:
    path = tmp_path / "guardrails.json"
    assert consolidator.load_guardrails(path) == []  # absent -> liste vide

    path.write_text(json.dumps([{"note": "stop obligatoire"}]), encoding="utf-8")
    assert consolidator.load_guardrails(path) == [{"note": "stop obligatoire"}]


def test_write_historise_la_version_remplacee(tmp_path) -> None:
    """Chaque consolidation écrase ~15 slots : la version remplacée doit être
    historisée en append-only pour audit/récupération (chantier learnings 02/07)."""
    store = consolidator.ConsolidatedLearningsStore(tmp_path / "learnings_consolidated.json")
    v1 = {"global": [{"note": "v1 : tenir les gagnants."}], "by_symbol": {}}
    v2 = {"global": [{"note": "v2 : couper vite les perdants."}], "by_symbol": {}}
    store.write(v1, watermark="2026-07-01T00:00:00+00:00")
    store.write(v2, watermark="2026-07-02T00:00:00+00:00")

    history = tmp_path / "archive" / "learnings_consolidated-history.jsonl"
    assert history.exists()
    rows = [json.loads(line) for line in history.read_text().splitlines()]
    assert len(rows) == 1  # le premier write ne remplace rien
    assert rows[0]["payload"]["global"][0]["note"] == "v1 : tenir les gagnants."
    assert rows[0]["replaced_by_watermark"] == "2026-07-02T00:00:00+00:00"
    assert "archived_at" in rows[0]
    # Le vif contient bien la v2
    assert store.read()["global"][0]["note"] == "v2 : couper vite les perdants."
