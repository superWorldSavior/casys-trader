"""Tests TDD pour trader.rotation_override."""
from __future__ import annotations


from trader.rotation_override import (
    build_override_prompt,
    make_llm_override_fn,
    parse_override,
)


# ---------------------------------------------------------------------------
# build_override_prompt
# ---------------------------------------------------------------------------

class TestBuildOverridePrompt:
    def _ranked(self, symbols: list[str]) -> list[dict]:
        return [
            {"symbol": s, "attractiveness": float(i), "bias": "LONG"}
            for i, s in enumerate(reversed(symbols), 1)
        ]

    def test_contains_default_hot_symbols(self):
        ranked = self._ranked(["AAPL", "MSFT", "TSLA"])
        prompt = build_override_prompt(ranked, ["AAPL", "MSFT"])
        assert "AAPL" in prompt
        assert "MSFT" in prompt

    def test_contains_candidate_symbols(self):
        ranked = self._ranked(["AAPL", "MSFT", "TSLA"])
        prompt = build_override_prompt(ranked, ["AAPL"])
        assert "TSLA" in prompt

    def test_respects_max_candidates(self):
        symbols = [f"SYM{i:03d}" for i in range(100)]
        ranked = self._ranked(symbols)
        # _ranked inverse la liste → ranked[0] = SYM099 (attractivité max)
        top10_symbols = [r["symbol"] for r in ranked[:10]]
        tail_symbols = [r["symbol"] for r in ranked[10:]]
        prompt = build_override_prompt(ranked, ["SYM099"], max_candidates=10)
        # Les 10 premiers du ranked doivent être dans le prompt
        for s in top10_symbols:
            assert s in prompt
        # Au moins un symbole au-delà des 10 premiers doit être absent
        excluded = [s for s in tail_symbols if s not in prompt]
        assert len(excluded) > 0

    def test_requests_json_response(self):
        ranked = self._ranked(["AAPL"])
        prompt = build_override_prompt(ranked, ["AAPL"])
        assert "add" in prompt.lower()
        assert "remove" in prompt.lower()
        assert "JSON" in prompt or "json" in prompt.lower()

    def test_includes_attractiveness_and_bias(self):
        ranked = [{"symbol": "AAPL", "attractiveness": 0.9, "bias": "LONG"}]
        prompt = build_override_prompt(ranked, [])
        assert "AAPL" in prompt
        # Au moins l'attractivité ou le bias doivent être mentionnés
        assert "0.9" in prompt or "LONG" in prompt


# ---------------------------------------------------------------------------
# parse_override
# ---------------------------------------------------------------------------

class TestParseOverride:
    def test_pure_json(self):
        text = '{"add": ["X"], "remove": ["Y"]}'
        result = parse_override(text)
        assert result == {"add": ["X"], "remove": ["Y"]}

    def test_json_in_markdown_block(self):
        text = '```json\n{"add": ["A", "B"], "remove": []}\n```'
        result = parse_override(text)
        assert result == {"add": ["A", "B"], "remove": []}

    def test_json_surrounded_by_text(self):
        text = 'Voici ma réponse : {"add": ["Z"], "remove": ["W"]} merci.'
        result = parse_override(text)
        assert result == {"add": ["Z"], "remove": ["W"]}

    def test_no_json_returns_empty(self):
        result = parse_override("pas de JSON ici")
        assert result == {"add": [], "remove": []}

    def test_invalid_json_returns_empty(self):
        result = parse_override("{add: [X]}")
        assert result == {"add": [], "remove": []}

    def test_json_missing_add_key_returns_empty_list(self):
        result = parse_override('{"remove": ["Y"]}')
        assert result["add"] == []
        assert result["remove"] == ["Y"]

    def test_json_missing_remove_key_returns_empty_list(self):
        result = parse_override('{"add": ["X"]}')
        assert result["add"] == ["X"]
        assert result["remove"] == []

    def test_both_keys_missing_returns_empty(self):
        result = parse_override('{"foo": "bar"}')
        assert result == {"add": [], "remove": []}

    def test_empty_lists(self):
        result = parse_override('{"add": [], "remove": []}')
        assert result == {"add": [], "remove": []}


# ---------------------------------------------------------------------------
# make_llm_override_fn
# ---------------------------------------------------------------------------

class TestMakeLlmOverrideFn:
    def _payload(self) -> dict:
        return {
            "ranked": [
                {"symbol": "AAPL", "attractiveness": 0.9, "bias": "LONG"},
                {"symbol": "TSLA", "attractiveness": 0.5, "bias": "LONG"},
            ],
            "default_hot": ["AAPL"],
        }

    def test_str_result_parsed_correctly(self):
        def complete_fn(prompt, *, timeout_s=120):
            return '{"add": ["X"], "remove": ["Y"]}'

        fn = make_llm_override_fn(complete_fn)
        result = fn(self._payload())
        assert result == {"add": ["X"], "remove": ["Y"]}

    def test_object_with_text_attribute(self):
        class FakeResponse:
            text = '{"add": ["A"], "remove": []}'

        def complete_fn(prompt, *, timeout_s=120):
            return FakeResponse()

        fn = make_llm_override_fn(complete_fn)
        result = fn(self._payload())
        assert result == {"add": ["A"], "remove": []}

    def test_complete_fn_raises_returns_empty(self):
        def complete_fn(prompt, *, timeout_s=120):
            raise RuntimeError("LLM timeout")

        fn = make_llm_override_fn(complete_fn)
        result = fn(self._payload())
        assert result == {"add": [], "remove": []}

    def test_complete_fn_returns_none_returns_empty(self):
        def complete_fn(prompt, *, timeout_s=120):
            return None

        fn = make_llm_override_fn(complete_fn)
        result = fn(self._payload())
        assert result == {"add": [], "remove": []}

    def test_timeout_forwarded(self):
        received = {}

        def complete_fn(prompt, *, timeout_s=120):
            received["timeout_s"] = timeout_s
            return '{"add": [], "remove": []}'

        fn = make_llm_override_fn(complete_fn, timeout_s=30)
        fn(self._payload())
        assert received["timeout_s"] == 30

    def test_max_candidates_forwarded(self):
        """max_candidates doit limiter le nombre de candidats dans le prompt."""
        received_prompts = []

        def complete_fn(prompt, *, timeout_s=120):
            received_prompts.append(prompt)
            return '{"add": [], "remove": []}'

        payload = {
            "ranked": [{"symbol": f"S{i}", "attractiveness": float(i), "bias": "LONG"} for i in range(20)],
            "default_hot": ["S0"],
        }

        fn = make_llm_override_fn(complete_fn, max_candidates=5)
        fn(payload)

        prompt = received_prompts[0]
        # Les 5 premiers doivent être présents
        for i in range(5):
            assert f"S{i}" in prompt


# ---------------------------------------------------------------------------
# B1 : build_override_prompt reçoit sticky → section "non retirables"
# ---------------------------------------------------------------------------


class TestBuildOverridePromptSticky:
    def _ranked(self, symbols: list[str]) -> list[dict]:
        return [
            {"symbol": s, "attractiveness": float(i), "bias": "LONG"}
            for i, s in enumerate(reversed(symbols), 1)
        ]

    def test_prompt_liste_les_sticky_comme_non_retirables(self):
        prompt = build_override_prompt(
            ranked=[{"symbol": "AAA", "attractiveness": 0.9, "bias": "LONG"},
                    {"symbol": "BBB", "attractiveness": 0.5, "bias": "LONG"}],
            default_hot=["AAA"],
            sticky={"ZZZ"},
        )
        assert "ZZZ" in prompt
        # le prompt doit indiquer que les sticky ne peuvent pas être retirés
        assert "sticky" in prompt.lower() or "non retirable" in prompt.lower() or "ne peux pas" in prompt.lower()

    def test_prompt_vide_sticky_pas_de_section(self):
        """Quand sticky est vide, le prompt ne plante pas."""
        prompt = build_override_prompt(
            ranked=[{"symbol": "AAA", "attractiveness": 0.9, "bias": "LONG"}],
            default_hot=["AAA"],
            sticky=set(),
        )
        # Doit toujours être un prompt valide demandant du JSON
        assert "add" in prompt.lower()
        assert "remove" in prompt.lower()

    def test_prompt_plusieurs_sticky_tous_presents(self):
        prompt = build_override_prompt(
            ranked=[{"symbol": "AAA", "attractiveness": 0.9, "bias": "LONG"}],
            default_hot=["AAA"],
            sticky={"SYM1", "SYM2", "SYM3"},
        )
        assert "SYM1" in prompt
        assert "SYM2" in prompt
        assert "SYM3" in prompt

    def test_make_llm_override_fn_transmet_sticky_au_prompt(self):
        """make_llm_override_fn extrait sticky du payload et le passe à build_override_prompt."""
        received_prompts = []

        def complete_fn(prompt, *, timeout_s=120):
            received_prompts.append(prompt)
            return '{"add": [], "remove": []}'

        fn = make_llm_override_fn(complete_fn)
        fn({
            "ranked": [{"symbol": "AAA", "attractiveness": 0.9, "bias": "LONG"}],
            "default_hot": ["AAA"],
            "sticky": {"ZZZ"},
        })
        assert len(received_prompts) == 1
        assert "ZZZ" in received_prompts[0]


# ---------------------------------------------------------------------------
# B2 : build_override_prompt reçoit market_context → section régime sectoriel
# ---------------------------------------------------------------------------


class TestBuildOverridePromptMarketContext:
    def test_prompt_inclut_le_regime_sectoriel(self):
        prompt = build_override_prompt(
            ranked=[{"symbol": "AAA", "attractiveness": 0.9, "bias": "LONG"}],
            default_hot=["AAA"],
            sticky=set(),
            market_context={"regime_families": {"defense": {"dir": "up", "frac": 0.8}}},
        )
        assert "defense" in prompt
        assert "up" in prompt.lower() or "haussier" in prompt.lower()

    def test_prompt_sans_market_context_ne_plante_pas(self):
        """market_context=None → le prompt est généré normalement."""
        prompt = build_override_prompt(
            ranked=[{"symbol": "AAA", "attractiveness": 0.9, "bias": "LONG"}],
            default_hot=["AAA"],
            sticky=set(),
            market_context=None,
        )
        assert "add" in prompt.lower()
        assert "remove" in prompt.lower()

    def test_prompt_market_context_vide_ne_plante_pas(self):
        prompt = build_override_prompt(
            ranked=[{"symbol": "AAA", "attractiveness": 0.9, "bias": "LONG"}],
            default_hot=["AAA"],
            sticky=set(),
            market_context={},
        )
        assert "add" in prompt.lower()

    def test_make_llm_override_fn_transmet_market_context(self):
        """make_llm_override_fn lit market_context depuis payload et le passe au prompt."""
        received_prompts = []

        def complete_fn(prompt, *, timeout_s=120):
            received_prompts.append(prompt)
            return '{"add": [], "remove": []}'

        fn = make_llm_override_fn(complete_fn)
        fn({
            "ranked": [{"symbol": "AAA", "attractiveness": 0.9, "bias": "LONG"}],
            "default_hot": ["AAA"],
            "sticky": set(),
            "market_context": {"regime_families": {"semis": {"dir": "down", "frac": 0.6}}},
        })
        assert len(received_prompts) == 1
        assert "semis" in received_prompts[0]
