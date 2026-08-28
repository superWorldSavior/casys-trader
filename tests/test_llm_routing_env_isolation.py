"""Isolation process-level des knobs de routage LLM injectés par load_dotenv."""

from __future__ import annotations

import os

from tests.conftest import _LLM_ROUTING_ENV_KEYS
from trader.agent.learnings import consolidator
from trader.agent.llm import AcpxBackend, build_default_router_from_env

_SENTINEL = "leaked-from-previous-test"
_BASELINE = {key: os.environ.get(key) for key in _LLM_ROUTING_ENV_KEYS}


def test_injection_dotenv_des_cles_de_routage_llm_est_visible_dans_le_test() -> None:
    """load_dotenv écrit os.environ directement, hors monkeypatch."""

    for key in _LLM_ROUTING_ENV_KEYS:
        os.environ[key] = _SENTINEL

    assert os.environ["TRADER_ACPX_AGENT"] == _SENTINEL
    assert os.environ["TRADER_MODEL"] == _SENTINEL
    assert os.environ["TRADER_FALLBACK_ACPX_AGENT"] == _SENTINEL
    assert os.environ["TRADER_CONSOLIDATOR_ACPX_AGENT"] == _SENTINEL


def test_builder_defaut_ne_voit_pas_linjection_dotenv_du_test_precedent() -> None:
    for key in _LLM_ROUTING_ENV_KEYS:
        assert os.environ.get(key) == _BASELINE[key], key

    brain = build_default_router_from_env(env_path=None)
    assert isinstance(brain.backends[0], AcpxBackend)
    assert brain.backends[0].agent is None
    assert brain.backends[0].model == "gpt-5.6-terra"
    assert brain.backends[1].provider == "acpx-claude-sonnet"
    assert brain.backends[1].agent == "claude"

    router = consolidator.build_consolidator_router_from_env(env_path=None)
    assert router.backends[0].agent == consolidator.DEFAULT_CONSOLIDATOR_ACPX_AGENT
    assert router.backends[0].model == consolidator.DEFAULT_CONSOLIDATOR_MODEL
