import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

import trader.agent.llm as llm
from trader.agent.llm import (
    AcpxBackend,
    LlmCompletion,
    LlmFailure,
    LlmRouter,
    OpenAICompatibleBackend,
    build_acpx_command,
    build_default_router_from_env,
    _looks_retryable_provider_error,
    _run_one_shot_command,
)
from trader.infrastructure.llm.acpx_backend import _validated_acpx_codex_home
from trader.infrastructure.llm.acpx_backend import _validated_acpx_grok_home
from trader.infrastructure.llm.acpx_backend import _validated_acpx_kimi_home


class StubBackend:
    def __init__(self, result):
        self.provider = "stub"
        self.model = "stub-model"
        self.result = result
        self.calls = 0

    def complete(self, prompt: str, *, timeout_s: int):
        self.calls += 1
        return self.result


def test_router_fallback_sur_echec_retryable() -> None:
    primary = StubBackend(
        LlmFailure(
            provider="acpx",
            model="gpt-5.5/medium",
            code="rate_limited",
            message="credit exhausted",
            retryable=True,
        )
    )
    fallback = StubBackend(
        LlmCompletion(
            provider="ollama-cloud",
            model="nemotron-3-nano:30b-cloud",
            text='{"ok":true}',
        )
    )

    result = LlmRouter([primary, fallback]).complete("prompt", timeout_s=12)

    assert isinstance(result, LlmCompletion)
    assert result.provider == "ollama-cloud"
    assert result.model == "nemotron-3-nano:30b-cloud"
    assert result.fallback_reason == "acpx:rate_limited"
    assert primary.calls == 1
    assert fallback.calls == 1


def test_llm_backends_sont_des_exports_facade_depuis_infrastructure() -> None:
    assert LlmRouter.__module__ == "trader.domain.llm"
    assert LlmCompletion.__module__ == "trader.domain.llm"
    assert LlmFailure.__module__ == "trader.domain.llm"
    assert llm.LlmCompletion is LlmCompletion
    assert llm.LlmFailure is LlmFailure

    assert AcpxBackend.__module__ == "trader.infrastructure.llm.acpx_backend"
    assert llm.AcpxSession.__module__ == "trader.infrastructure.llm.acpx_backend"
    assert build_acpx_command.__module__ == "trader.infrastructure.llm.acpx_backend"
    assert _run_one_shot_command.__module__ == "trader.infrastructure.llm.acpx_backend"
    assert OpenAICompatibleBackend.__module__ == "trader.infrastructure.llm.openai_backend"


def test_router_ne_fallback_pas_sur_echec_non_retryable() -> None:
    primary = StubBackend(
        LlmFailure(
            provider="acpx",
            model="gpt-5.5/medium",
            code="bad_output",
            message="invalid JSON",
            retryable=False,
        )
    )
    fallback = StubBackend(
        LlmCompletion(
            provider="ollama-cloud",
            model="nemotron-3-nano:30b-cloud",
            text='{"ok":true}',
        )
    )

    result = LlmRouter([primary, fallback]).complete("prompt", timeout_s=12)

    assert isinstance(result, LlmFailure)
    assert result.code == "bad_output"
    assert fallback.calls == 0


def test_router_ne_fallback_pas_sur_timeout_non_retryable() -> None:
    primary = StubBackend(
        LlmFailure(
            provider="acpx",
            model="gpt-5.5/medium",
            code="timeout",
            message="> 900s",
            retryable=False,
        )
    )
    fallback = StubBackend(
        LlmCompletion(
            provider="ollama-cloud",
            model="nemotron-3-nano:30b-cloud",
            text='{"ok":true}',
        )
    )

    result = LlmRouter([primary, fallback]).complete("prompt", timeout_s=900)

    assert isinstance(result, LlmFailure)
    assert result.code == "timeout"
    assert fallback.calls == 0


def test_retryable_provider_error_se_limite_aux_rate_limits_et_quotas() -> None:
    assert _looks_retryable_provider_error("HTTP 429")
    assert _looks_retryable_provider_error("rate limit exceeded")
    assert _looks_retryable_provider_error("insufficient_quota")
    assert _looks_retryable_provider_error("quota exhausted")
    assert not _looks_retryable_provider_error("timeout")
    assert not _looks_retryable_provider_error("timed out")
    assert not _looks_retryable_provider_error("overloaded")
    assert not _looks_retryable_provider_error("erreur fournisseur generique")


def test_acpx_backend_timeout_est_retryable_et_cape_par_appel(monkeypatch) -> None:
    """Un timeout d'appel est TRANSITOIRE → retryable (incident AMCR 2026-07-06),
    et le subprocess est tué au cap par-appel (150s) et non au budget total (900s)."""
    # Le cap par-appel lit CASYS_ACPX_CALL_TIMEOUT_S ; l'isoler d'un .env chargé
    # par un autre test de la suite (sinon le cap observé n'est plus 150).
    monkeypatch.delenv("CASYS_ACPX_CALL_TIMEOUT_S", raising=False)
    # Le cap par-appel lit CASYS_ACPX_CALL_TIMEOUT_S ; l'isoler d'un .env chargé
    # par un autre test de la suite (sinon le cap observé n'est plus 150).
    monkeypatch.delenv("CASYS_ACPX_CALL_TIMEOUT_S", raising=False)
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.shutil.which", lambda _bin: "/usr/local/bin/acpx")
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._terminate_process_group", lambda _pid: None)

    seen: dict[str, float | None] = {"timeout": None}

    class TimeoutPopen:
        pid = 4242
        returncode = None

        def __init__(self, command, **kwargs):
            self.command = command

        def communicate(self, timeout=None):
            # 1er appel = le budget réel ; le 2e (cleanup post-kill) vaut 1s.
            if seen["timeout"] is None:
                seen["timeout"] = timeout
            raise subprocess.TimeoutExpired(cmd=self.command, timeout=timeout)

        def kill(self):
            return None

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.subprocess.Popen", TimeoutPopen)

    result = AcpxBackend().complete("prompt", timeout_s=900)

    assert isinstance(result, LlmFailure)
    assert result.provider == "acpx"
    assert result.code == "timeout"
    assert result.retryable is True
    # budget par-appel = min(900, cap 150) → subprocess tué à 150 + 15 grace.
    assert seen["timeout"] == 150 + 15
    assert result.message == "> 150s"


def test_per_call_timeout_cap_configurable(monkeypatch) -> None:
    monkeypatch.delenv("CASYS_ACPX_CALL_TIMEOUT_S", raising=False)
    assert llm._per_call_timeout_cap_s() == 150
    monkeypatch.setenv("CASYS_ACPX_CALL_TIMEOUT_S", "90")
    assert llm._per_call_timeout_cap_s() == 90
    monkeypatch.setenv("CASYS_ACPX_CALL_TIMEOUT_S", "pas-un-int")
    assert llm._per_call_timeout_cap_s() == 150


def test_acpx_call_journalise_session_pid_outcome(monkeypatch, caplog) -> None:
    """Le journal [acpx_call] porte session/pid/outcome pour tracer chaque appel."""
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.shutil.which", lambda _bin: "/usr/local/bin/acpx")
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._terminate_process_group", lambda _pid: None)

    class OkPopen:
        pid = 7777
        returncode = 0

        def __init__(self, command, **kwargs):
            self.command = command

        def communicate(self, timeout=None):
            return ('{"AMCR": {}}', "")

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.subprocess.Popen", OkPopen)

    session = llm.AcpxSession(
        provider="acpx", model="gpt-5.5", acpx_bin="acpx", name="304:0"
    )
    with caplog.at_level("INFO", logger="trader.infrastructure.llm.acpx_backend"):
        session.send("prompt", timeout_s=240, call_ctx={"symbol": "AMCR"})

    line = next(m for m in caplog.messages if m.startswith("[acpx_call]"))
    assert "session=304:0" in line
    assert "symbol=AMCR" in line
    assert "pid=7777" in line
    assert "outcome=ok" in line


def test_acpx_backend_consolidateur_traite_internal_error_comme_retryable(monkeypatch) -> None:
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.shutil.which", lambda _bin: "/usr/local/bin/acpx")
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._terminate_process_group", lambda _pid: None)

    class InternalErrorPopen:
        pid = 4242
        returncode = 1

        def __init__(self, command, **kwargs):
            self.command = command

        def communicate(self, timeout=None):
            return "", "Internal error\n"

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.subprocess.Popen", InternalErrorPopen)

    result = AcpxBackend(provider="consolidator", model="gpt-5.5/high").complete("prompt", timeout_s=240)

    assert isinstance(result, LlmFailure)
    assert result.retryable is True
    assert result.code == "provider_error"


def test_acpx_backend_runtime_traite_internal_error_comme_retryable(monkeypatch) -> None:
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.shutil.which", lambda _bin: "/usr/local/bin/acpx")
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._terminate_process_group", lambda _pid: None)

    class InternalErrorPopen:
        pid = 4242
        returncode = 1

        def __init__(self, command, **kwargs):
            self.command = command

        def communicate(self, timeout=None):
            return "", "Internal error\n"

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.subprocess.Popen", InternalErrorPopen)

    result = AcpxBackend(provider="acpx", model="gpt-5.5").complete("prompt", timeout_s=240)

    assert isinstance(result, LlmFailure)
    assert result.retryable is True
    assert result.code == "provider_error"


def test_acpx_backend_exit_non_zero_sans_sortie_est_retryable(monkeypatch) -> None:
    """exit!=0 acpx SANS aucune sortie (stderr+stdout vides) = blip provider
    transitoire (quota/dispo qui hoquette), donc retryable -> le routeur peut
    tomber en fallback au lieu de figer un HOLD sec."""
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.shutil.which", lambda _bin: "/usr/local/bin/acpx")
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._terminate_process_group", lambda _pid: None)

    class EmptyExitPopen:
        pid = 4242
        returncode = 1

        def __init__(self, command, **kwargs):
            self.command = command

        def communicate(self, timeout=None):
            return "", ""

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.subprocess.Popen", EmptyExitPopen)

    result = AcpxBackend(provider="acpx", model="gpt-5.5").complete("prompt", timeout_s=240)

    assert isinstance(result, LlmFailure)
    assert result.retryable is True
    assert result.code == "provider_error"


def test_acpx_backend_exit_non_zero_avec_erreur_explicite_reste_non_retryable(monkeypatch) -> None:
    """Garde-fou : un exit!=0 avec un message d'erreur explicite et NON transitoire
    (ni rate-limit, ni internal-error, ni sortie vide) reste un nonzero_exit
    non-retryable -- on ne doit pas tout rendre retryable."""
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.shutil.which", lambda _bin: "/usr/local/bin/acpx")
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._terminate_process_group", lambda _pid: None)

    class ExplicitErrorPopen:
        pid = 4242
        returncode = 1

        def __init__(self, command, **kwargs):
            self.command = command

        def communicate(self, timeout=None):
            return "", "fatal: configuration invalide\n"

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.subprocess.Popen", ExplicitErrorPopen)

    result = AcpxBackend(provider="acpx", model="gpt-5.5").complete("prompt", timeout_s=240)

    assert isinstance(result, LlmFailure)
    assert result.retryable is False
    assert result.code == "nonzero_exit"


def test_acpx_session_send_retourne_une_completion_sur_stdout(monkeypatch) -> None:
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.shutil.which", lambda _bin: "/usr/local/bin/acpx")
    calls = []

    def fake_run(command, *, timeout_s, on_pid=None, codex_home=None, agent=None):
        calls.append((command, timeout_s))
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout='{"symbol":"SPY","action":"HOLD"}',
            stderr="",
        )

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._run_one_shot_command", fake_run)

    session = llm.AcpxSession(
        provider="acpx",
        model="gpt-5.5/medium",
        acpx_bin="acpx",
        name="casys-trader:runtime-brain:0",
    )

    result = session.send("analyse ce symbole", timeout_s=45)

    assert isinstance(result, LlmCompletion)
    assert result.provider == "acpx"
    assert result.model == "gpt-5.5/medium"
    assert result.text == '{"symbol":"SPY","action":"HOLD"}'
    assert calls == [
        (
            [
                "acpx",
                "--format",
                "quiet",
                "--allowed-tools",
                "",
                "--no-terminal",
                "--non-interactive-permissions",
                "deny",
                "--model",
                "gpt-5.5/medium",
                "--timeout",
                "45",
                "prompt",
                "-s",
                "casys-trader:runtime-brain:0",
                "analyse ce symbole",
            ],
            60,
        )
    ]


def test_acpx_backend_open_session_cree_une_session_neuve_et_retourne_un_objet(monkeypatch) -> None:
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.shutil.which", lambda _bin: "/usr/local/bin/acpx")
    calls = []

    def fake_run(command, *, timeout_s, on_pid=None, codex_home=None, agent=None):
        calls.append((command, timeout_s))
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._run_one_shot_command", fake_run)

    backend = AcpxBackend(provider="acpx", model="gpt-5.5/medium", acpx_bin="acpx")

    result = backend.open_session("casys-trader:runtime-brain:0", timeout_s=30)

    assert isinstance(result, llm.AcpxSession)
    assert result.provider == "acpx"
    assert result.model == "gpt-5.5/medium"
    assert result.acpx_bin == "acpx"
    assert result.name == "casys-trader:runtime-brain:0"
    assert result.agent is None
    assert calls == [
        (
            [
                "acpx",
                "--format",
                "quiet",
                "--allowed-tools",
                "",
                "--no-terminal",
                "--non-interactive-permissions",
                "deny",
                "--model",
                "gpt-5.5/medium",
                "--timeout",
                "30",
                "sessions",
                "new",
                "-s",
                "casys-trader:runtime-brain:0",
            ],
            45,
        )
    ]


def test_acpx_backend_applique_l_effort_sur_la_session_avant_le_prompt(monkeypatch) -> None:
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.shutil.which", lambda _bin: "/usr/local/bin/acpx")
    calls = []

    def fake_run(command, *, timeout_s, on_pid=None, codex_home=None, agent=None):
        calls.append((command, timeout_s))
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="medium", stderr="")

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._run_one_shot_command", fake_run)
    backend = AcpxBackend(
        provider="acpx",
        model="gpt-5.6-luna",
        acpx_bin="acpx",
        reasoning_effort="medium",
    )

    result = backend.open_session("casys-trader:runtime-brain:0", timeout_s=30)

    assert isinstance(result, llm.AcpxSession)
    assert calls[1] == (
        [
            "acpx",
            "--format",
            "quiet",
            "--no-terminal",
            "--non-interactive-permissions",
            "deny",
            "set",
            "reasoning_effort",
            "medium",
            "--session",
            "casys-trader:runtime-brain:0",
        ],
        45,
    )


def test_acpx_backend_open_session_retourne_l_echec_si_new_echoue(monkeypatch) -> None:
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.shutil.which", lambda _bin: "/usr/local/bin/acpx")

    def fake_run(command, *, timeout_s, on_pid=None, codex_home=None, agent=None):
        return subprocess.CompletedProcess(
            args=command,
            returncode=1,
            stdout="",
            stderr="fatal: configuration invalide\n",
        )

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._run_one_shot_command", fake_run)

    backend = AcpxBackend(provider="acpx", model="gpt-5.5/medium", acpx_bin="acpx")

    result = backend.open_session("casys-trader:runtime-brain:0", timeout_s=30)

    assert isinstance(result, LlmFailure)
    assert result.provider == "acpx"
    assert result.model == "gpt-5.5/medium"
    assert result.retryable is False
    assert result.code == "nonzero_exit"


def test_acpx_backend_agent_claude_est_porte_par_session_new_et_prompt(monkeypatch) -> None:
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.shutil.which", lambda _bin: "/usr/local/bin/acpx")
    calls = []

    def fake_run(command, *, timeout_s, on_pid=None, codex_home=None, agent=None):
        calls.append((command, timeout_s))
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._run_one_shot_command", fake_run)

    backend = AcpxBackend(
        provider="acpx-claude-sonnet",
        model="claude-sonnet",
        acpx_bin="acpx",
        agent="claude",
    )

    session = backend.open_session("casys-trader:runtime-brain:0", timeout_s=30)
    assert isinstance(session, llm.AcpxSession)
    assert session.agent == "claude"

    result = session.send("analyse ce symbole", timeout_s=12)

    assert isinstance(result, llm.LlmCompletion)
    assert calls[0][0][-5:] == ["claude", "sessions", "new", "-s", "casys-trader:runtime-brain:0"]
    assert calls[1][0][-5:] == ["claude", "prompt", "-s", "casys-trader:runtime-brain:0", "analyse ce symbole"]


def test_acpx_session_close_envoie_la_commande_de_fermeture(monkeypatch) -> None:
    calls = []

    def fake_run(command, *, timeout_s, on_pid=None, codex_home=None, agent=None):
        calls.append(command)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._run_one_shot_command", fake_run)

    session = llm.AcpxSession(
        provider="acpx",
        model="gpt-5.5/medium",
        acpx_bin="acpx",
        name="casys-trader:runtime-brain:0",
    )

    session.close()

    assert calls == [
        [
            "acpx",
            "--format",
            "quiet",
            "--no-terminal",
            "--non-interactive-permissions",
            "deny",
            "sessions",
            "close",
            "casys-trader:runtime-brain:0",
        ]
    ]


def test_acpx_session_close_logge_un_warning_sans_lever(monkeypatch, caplog) -> None:
    def fake_run(command, *, timeout_s, on_pid=None, codex_home=None, agent=None):
        raise RuntimeError("acpx close failed")

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._run_one_shot_command", fake_run)
    caplog.set_level("WARNING", logger="trader.infrastructure.llm.acpx_backend")

    session = llm.AcpxSession(
        provider="acpx",
        model="gpt-5.5/medium",
        acpx_bin="acpx",
        name="casys-trader:runtime-brain:0",
    )

    session.close()

    assert "acpx close failed" in caplog.text
    assert "casys-trader:runtime-brain:0" in caplog.text


def test_acpx_session_close_peut_etre_appele_deux_fois_sans_lever(monkeypatch) -> None:
    calls = []

    def fake_run(command, *, timeout_s, on_pid=None, codex_home=None, agent=None):
        calls.append(command)
        if len(calls) == 2:
            raise RuntimeError("already closed")
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._run_one_shot_command", fake_run)

    session = llm.AcpxSession(
        provider="acpx",
        model="gpt-5.5/medium",
        acpx_bin="acpx",
        name="casys-trader:runtime-brain:0",
    )

    session.close()
    session.close()

    assert len(calls) == 2


def test_run_with_session_fallback_happy_path_retourne_le_resultat_et_ferme_la_session() -> None:
    events = []

    class FakeSession:
        def __init__(self, name):
            self.name = name

        def close(self):
            events.append(("close", self.name))

    class FakeBackend:
        def open_session(self, name, *, timeout_s):
            events.append(("open", name, timeout_s))
            return FakeSession(name)

    def resolve(session):
        events.append(("resolve", session.name))
        return "DECISION"

    result = llm.run_with_session_fallback(
        [FakeBackend()],
        task_id="resolve:SPY",
        resolve=resolve,
        open_timeout_s=12,
    )

    assert result == "DECISION"
    assert events == [
        ("open", "resolve:SPY:0", 12),
        ("resolve", "resolve:SPY:0"),
        ("close", "resolve:SPY:0"),
    ]


def test_session_complete_fn_retourne_la_completion_de_la_session() -> None:
    completion = LlmCompletion(provider="acpx", model="gpt-5.5", text='{"ok":true}')
    calls = []

    class FakeSession:
        def send(self, prompt, *, timeout_s, call_ctx=None):
            calls.append((prompt, timeout_s))
            return completion

    complete = llm.session_complete_fn(FakeSession())

    result = complete("prompt", 42)

    assert result is completion
    assert calls == [("prompt", 42)]


def test_session_complete_fn_leve_provider_down_sur_echec_retryable() -> None:
    failure = LlmFailure(
        provider="acpx",
        model="gpt-5.5",
        code="rate_limited",
        message="quota",
        retryable=True,
    )

    class FakeSession:
        def send(self, prompt, *, timeout_s, call_ctx=None):
            return failure

    complete = llm.session_complete_fn(FakeSession())

    with pytest.raises(llm.SessionProviderDown) as exc_info:
        complete("prompt", 42)

    assert exc_info.value.failure is failure


def test_session_complete_fn_retourne_l_echec_non_retryable() -> None:
    failure = LlmFailure(
        provider="acpx",
        model="gpt-5.5",
        code="bad_output",
        message="json invalide",
        retryable=False,
    )

    class FakeSession:
        def send(self, prompt, *, timeout_s, call_ctx=None):
            return failure

    complete = llm.session_complete_fn(FakeSession())

    assert complete("prompt", 42) is failure


def test_run_with_session_fallback_open_failure_passe_au_backend_suivant() -> None:
    events = []
    failure = LlmFailure(
        provider="acpx",
        model="gpt-5.5",
        code="rate_limited",
        message="quota",
        retryable=True,
    )

    class FakeSession:
        def __init__(self, name):
            self.name = name

        def close(self):
            events.append(("close", self.name))

    class FailingOpenBackend:
        def open_session(self, name, *, timeout_s):
            events.append(("open", name, timeout_s))
            return failure

    class WorkingBackend:
        def open_session(self, name, *, timeout_s):
            events.append(("open", name, timeout_s))
            return FakeSession(name)

    def resolve(session):
        events.append(("resolve", session.name))
        return "DECISION"

    result = llm.run_with_session_fallback(
        [FailingOpenBackend(), WorkingBackend()],
        task_id="resolve:SPY",
        resolve=resolve,
        open_timeout_s=12,
    )

    assert result == "DECISION"
    assert events == [
        ("open", "resolve:SPY:0", 12),
        ("open", "resolve:SPY:1", 12),
        ("resolve", "resolve:SPY:1"),
        ("close", "resolve:SPY:1"),
    ]


def test_run_with_session_fallback_provider_down_restart_depuis_zero() -> None:
    events = []
    failure = LlmFailure(
        provider="acpx",
        model="gpt-5.5",
        code="rate_limited",
        message="quota",
        retryable=True,
    )

    class FakeSession:
        def __init__(self, name):
            self.name = name

        def close(self):
            events.append(("close", self.name))

    class FakeBackend:
        def open_session(self, name, *, timeout_s):
            events.append(("open", name, timeout_s))
            return FakeSession(name)

    def resolve(session):
        events.append(("resolve", session.name))
        if session.name.endswith(":0"):
            raise llm.SessionProviderDown(failure)
        return "DECISION"

    result = llm.run_with_session_fallback(
        [FakeBackend(), FakeBackend()],
        task_id="resolve:SPY",
        resolve=resolve,
        open_timeout_s=12,
    )

    assert result == "DECISION"
    assert events == [
        ("open", "resolve:SPY:0", 12),
        ("resolve", "resolve:SPY:0"),
        ("close", "resolve:SPY:0"),
        ("open", "resolve:SPY:1", 12),
        ("resolve", "resolve:SPY:1"),
        ("close", "resolve:SPY:1"),
    ]


def test_run_with_session_fallback_stamp_fallback_reason_sur_dataclass_compatible() -> None:
    failure = LlmFailure(
        provider="acpx",
        model="gpt-5.5",
        code="rate_limited",
        message="quota",
        retryable=True,
    )

    @dataclass(frozen=True)
    class DecisionLike:
        symbol: str
        llm_fallback_reason: str | None = None

    class FakeSession:
        def __init__(self, name):
            self.name = name

        def close(self):
            return None

    class FakeBackend:
        def open_session(self, name, *, timeout_s):
            return FakeSession(name)

    def resolve(session):
        if session.name.endswith(":0"):
            raise llm.SessionProviderDown(failure)
        return DecisionLike(symbol="SPY")

    result = llm.run_with_session_fallback(
        [FakeBackend(), FakeBackend()],
        task_id="resolve:SPY",
        resolve=resolve,
        open_timeout_s=12,
    )

    assert isinstance(result, DecisionLike)
    assert result.llm_fallback_reason == "acpx:rate_limited"


def test_run_with_session_fallback_ne_touche_pas_un_objet_sans_fallback_reason() -> None:
    failure = LlmFailure(
        provider="acpx",
        model="gpt-5.5",
        code="rate_limited",
        message="quota",
        retryable=True,
    )

    @dataclass(frozen=True)
    class PlainResult:
        value: str

    class FakeSession:
        def __init__(self, name):
            self.name = name

        def close(self):
            return None

    class FakeBackend:
        def open_session(self, name, *, timeout_s):
            return FakeSession(name)

    plain = PlainResult("ok")

    def resolve(session):
        if session.name.endswith(":0"):
            raise llm.SessionProviderDown(failure)
        return plain

    result = llm.run_with_session_fallback(
        [FakeBackend(), FakeBackend()],
        task_id="resolve:SPY",
        resolve=resolve,
        open_timeout_s=12,
    )

    assert result is plain


def test_run_with_session_fallback_tous_down_retourne_le_dernier_echec() -> None:
    events = []
    first_failure = LlmFailure(
        provider="acpx",
        model="gpt-5.5",
        code="rate_limited",
        message="quota primary",
        retryable=True,
    )
    last_failure = LlmFailure(
        provider="acpx-claude-sonnet",
        model="sonnet",
        code="provider_error",
        message="fallback down",
        retryable=True,
    )

    class FakeSession:
        def __init__(self, name, failure):
            self.name = name
            self.failure = failure

        def close(self):
            events.append(("close", self.name))

    class FakeBackend:
        def __init__(self, failure):
            self.failure = failure

        def open_session(self, name, *, timeout_s):
            events.append(("open", name, timeout_s))
            return FakeSession(name, self.failure)

    def resolve(session):
        events.append(("resolve", session.name))
        raise llm.SessionProviderDown(session.failure)

    result = llm.run_with_session_fallback(
        [FakeBackend(first_failure), FakeBackend(last_failure)],
        task_id="resolve:SPY",
        resolve=resolve,
        open_timeout_s=12,
    )

    assert result is last_failure
    assert events == [
        ("open", "resolve:SPY:0", 12),
        ("resolve", "resolve:SPY:0"),
        ("close", "resolve:SPY:0"),
        ("open", "resolve:SPY:1", 12),
        ("resolve", "resolve:SPY:1"),
        ("close", "resolve:SPY:1"),
    ]


def test_run_with_session_fallback_court_circuite_si_le_premier_backend_reussit() -> None:
    events = []

    class FakeSession:
        def __init__(self, name):
            self.name = name

        def close(self):
            events.append(("close", self.name))

    class FirstBackend:
        def open_session(self, name, *, timeout_s):
            events.append(("open-primary", name, timeout_s))
            return FakeSession(name)

    class SecondBackend:
        def open_session(self, name, *, timeout_s):
            events.append(("open-fallback", name, timeout_s))
            return FakeSession(name)

    def resolve(session):
        events.append(("resolve", session.name))
        return "PRIMARY_DECISION"

    result = llm.run_with_session_fallback(
        [FirstBackend(), SecondBackend()],
        task_id="resolve:SPY",
        resolve=resolve,
        open_timeout_s=12,
    )

    assert result == "PRIMARY_DECISION"
    assert events == [
        ("open-primary", "resolve:SPY:0", 12),
        ("resolve", "resolve:SPY:0"),
        ("close", "resolve:SPY:0"),
    ]


def test_acpx_backend_isole_et_nettoie_le_process_group(monkeypatch) -> None:
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.shutil.which", lambda _bin: "/usr/local/bin/acpx")
    popen_calls = []
    cleaned_pids = []

    class FakePopen:
        pid = 4242
        returncode = 0

        def __init__(self, command, **kwargs):
            popen_calls.append((command, kwargs))

        def communicate(self, timeout=None):
            return "OK", ""

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.subprocess.Popen", FakePopen)
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._terminate_process_group", lambda pid: cleaned_pids.append(pid))

    result = AcpxBackend().complete("prompt", timeout_s=12)

    assert isinstance(result, LlmCompletion)
    assert result.text == "OK"
    assert popen_calls[0][1]["stdout"] is subprocess.PIPE
    assert popen_calls[0][1]["stderr"] is subprocess.PIPE
    assert popen_calls[0][1]["text"] is True
    assert popen_calls[0][1]["start_new_session"] is True
    assert cleaned_pids == [4242]


def test_run_one_shot_nettoie_l_environnement_runtime_pollue(monkeypatch) -> None:
    monkeypatch.setenv("MallocStackLogging", "0")
    monkeypatch.setenv("MallocStackLoggingNoCompact", "1")
    monkeypatch.setenv("TRADER_OLLAMA_MODEL", "nemotron-3-ultra:cloud")
    monkeypatch.setenv("PATH", "/tmp/codex-path:/opt/homebrew/bin:/var/run/com.apple.security.cryptexd/codex.system/bootstrap/usr/bin:/usr/bin")
    captured = {}

    class FakePopen:
        pid = 4242
        returncode = 0

        def __init__(self, command, **kwargs):
            self.command = command
            captured.update(kwargs)

        def communicate(self, timeout=None):
            return "OK", ""

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.subprocess.Popen", FakePopen)
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._terminate_process_group", lambda _pid: None)

    result = _run_one_shot_command(["acpx", "exec", "prompt"], timeout_s=12)

    assert result.returncode == 0
    env = captured["env"]
    assert "MallocStackLogging" not in env
    assert "MallocStackLoggingNoCompact" not in env
    assert env["TRADER_OLLAMA_MODEL"] == "nemotron-3-ultra:cloud"
    assert env["PATH"] == "/opt/homebrew/bin:/usr/bin"


def test_run_one_shot_impose_le_profil_app_low(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    captured = {}

    class FakePopen:
        pid = 4242
        returncode = 0

        def __init__(self, command, **kwargs):
            captured.update(kwargs)

        def communicate(self, timeout=None):
            return "OK", ""

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.subprocess.Popen", FakePopen)
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._terminate_process_group", lambda _pid: None)

    result = _run_one_shot_command(["acpx", "exec", "prompt"], timeout_s=12)

    assert result.returncode == 0
    codex_home = Path(captured["env"]["CODEX_HOME"])
    assert codex_home == Path(__file__).resolve().parents[1] / "ops" / "codex-home"
    assert 'model_reasoning_effort = "low"' in (codex_home / "config.toml").read_text(encoding="utf-8")


@pytest.mark.parametrize("effort", ["medium", "high", "max", "xhigh", "ultra"])
def test_acpx_refuse_un_profil_hors_des_efforts_autorises(monkeypatch, tmp_path, effort) -> None:
    """La garde reste fail-close : seul le profil low actif passe au boot."""

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        f'model = "gpt-5.6-terra"\nmodel_reasoning_effort = "{effort}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.shutil.which", lambda _bin: "/usr/local/bin/acpx")

    result = AcpxBackend(model="gpt-5.6-terra").complete("prompt", timeout_s=12)

    assert isinstance(result, LlmFailure)
    assert "appel ACPX refusé" in result.message
    assert effort in result.message


@pytest.mark.parametrize("effort", ["low"])
def test_acpx_accepte_les_efforts_explicites_autorises(monkeypatch, tmp_path, effort) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        f'model = "gpt-5.6-terra"\nmodel_reasoning_effort = "{effort}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert _validated_acpx_codex_home() == codex_home


def test_codex_home_du_backend_prime_sur_l_env(monkeypatch, tmp_path) -> None:
    """Un profil explicitement demandé prime toujours sur le profil global."""

    low_home = tmp_path / "codex-home-low"
    low_home.mkdir()
    (low_home / "config.toml").write_text(
        'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "low"\n', encoding="utf-8"
    )
    global_home = tmp_path / "codex-home"
    global_home.mkdir()
    (global_home / "config.toml").write_text(
        'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "low"\n', encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(global_home))
    captured = {}

    class FakePopen:
        pid = 4242
        returncode = 0

        def __init__(self, command, **kwargs):
            captured.update(kwargs)

        def communicate(self, timeout=None):
            return "OK", ""

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.subprocess.Popen", FakePopen)
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._terminate_process_group", lambda _pid: None)

    _run_one_shot_command(["acpx", "exec", "prompt"], timeout_s=12, codex_home=str(low_home))

    assert Path(captured["env"]["CODEX_HOME"]) == low_home


def _fake_popen_capturant(monkeypatch, captured: dict):
    class FakePopen:
        pid = 4242
        returncode = 0

        def __init__(self, command, **kwargs):
            captured.update(kwargs)

        def communicate(self, timeout=None):
            return "OK", ""

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.subprocess.Popen", FakePopen)
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._terminate_process_group", lambda _pid: None)


def _profil_codex_valide(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(
        'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "low"\n', encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def test_kimi_home_absent_retombe_sur_le_profil_de_l_app(monkeypatch) -> None:
    """Jamais ~/.kimi-code : sans la var, le défaut est le profil versionné.

    C'est l'invariant qui compte — un ``KIMI_CODE_HOME`` manquant ferait sinon
    tourner le daemon sur le CLI personnel EN SILENCE (modèle et effort de la
    machine du dev, pas ceux de l'app).
    """

    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)

    resolved = _validated_acpx_kimi_home()

    assert resolved.name == "kimi-home"
    assert resolved.parent.name == "ops"
    assert resolved != Path.home() / ".kimi-code"


@pytest.mark.parametrize("effort", ["low", "medium", ""])
def test_kimi_effort_faible_est_refuse(monkeypatch, tmp_path, effort) -> None:
    kimi_home = tmp_path / "kimi-home"
    kimi_home.mkdir()
    body = f'[thinking]\neffort = "{effort}"\n' if effort else "[thinking]\nenabled = true\n"
    (kimi_home / "config.toml").write_text(body, encoding="utf-8")
    monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_home))

    with pytest.raises(RuntimeError, match="appel ACPX refusé"):
        _validated_acpx_kimi_home()


@pytest.mark.parametrize("effort", ["high", "max"])
def test_kimi_accepte_les_deux_crans_hauts(monkeypatch, tmp_path, effort) -> None:
    kimi_home = tmp_path / "kimi-home"
    kimi_home.mkdir()
    (kimi_home / "config.toml").write_text(
        f'[thinking]\neffort = "{effort}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_home))

    assert _validated_acpx_kimi_home() == kimi_home


def test_kimi_home_illisible_est_refuse(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "profil-inexistant"))

    with pytest.raises(RuntimeError, match="profil ACPX kimi illisible"):
        _validated_acpx_kimi_home()


def test_agent_kimi_pose_son_profil_dans_le_subprocess(monkeypatch, tmp_path) -> None:
    """Le profil validé est celui de l'agent RÉELLEMENT lancé, pas juste Codex."""

    _profil_codex_valide(tmp_path, monkeypatch)
    kimi_home = tmp_path / "kimi-home"
    kimi_home.mkdir()
    (kimi_home / "config.toml").write_text('[thinking]\neffort = "high"\n', encoding="utf-8")
    monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_home))
    captured: dict = {}
    _fake_popen_capturant(monkeypatch, captured)

    _run_one_shot_command(["acpx", "kimi", "exec", "prompt"], timeout_s=12, agent="kimi")

    assert Path(captured["env"]["KIMI_CODE_HOME"]) == kimi_home


def test_agent_kimi_avec_profil_invalide_refuse_avant_le_subprocess(monkeypatch, tmp_path) -> None:
    _profil_codex_valide(tmp_path, monkeypatch)
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "profil-inexistant"))

    def jamais_lance(*_args, **_kwargs):
        raise AssertionError("le subprocess ne doit pas démarrer sur un profil kimi invalide")

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.subprocess.Popen", jamais_lance)

    with pytest.raises(RuntimeError, match="profil ACPX kimi illisible"):
        _run_one_shot_command(["acpx", "kimi", "exec", "prompt"], timeout_s=12, agent="kimi")


def test_agent_codex_n_impose_pas_le_profil_kimi(monkeypatch, tmp_path) -> None:
    """La garde kimi ne doit pas s'armer sous codex."""

    _profil_codex_valide(tmp_path, monkeypatch)
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "profil-inexistant"))
    captured: dict = {}
    _fake_popen_capturant(monkeypatch, captured)

    result = _run_one_shot_command(["acpx", "codex", "exec", "prompt"], timeout_s=12, agent="codex")

    assert result.returncode == 0


def test_grok_home_absent_retombe_sur_le_profil_de_l_app(monkeypatch) -> None:
    monkeypatch.delenv("GROK_HOME", raising=False)

    resolved = _validated_acpx_grok_home()

    assert resolved.name == "grok-home"
    assert resolved.parent.name == "ops"
    assert resolved != Path.home() / ".grok"


def test_grok_effort_absent_est_refuse(monkeypatch, tmp_path) -> None:
    grok_home = tmp_path / "grok-home"
    grok_home.mkdir()
    (grok_home / "config.toml").write_text("[models]\ndefault = \"grok-4.6\"\n", encoding="utf-8")
    monkeypatch.setenv("GROK_HOME", str(grok_home))

    with pytest.raises(RuntimeError, match="appel ACPX refusé"):
        _validated_acpx_grok_home()


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
def test_grok_accepte_les_efforts_annonces(monkeypatch, tmp_path, effort) -> None:
    grok_home = tmp_path / "grok-home"
    grok_home.mkdir()
    (grok_home / "config.toml").write_text(
        f"[models]\ndefault_reasoning_effort = \"{effort}\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GROK_HOME", str(grok_home))

    assert _validated_acpx_grok_home() == grok_home


def test_agent_grok_pose_son_profil_dans_le_subprocess(monkeypatch, tmp_path) -> None:
    _profil_codex_valide(tmp_path, monkeypatch)
    grok_home = tmp_path / "grok-home"
    grok_home.mkdir()
    (grok_home / "config.toml").write_text(
        "[models]\ndefault_reasoning_effort = \"medium\"\n", encoding="utf-8"
    )
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    captured: dict = {}
    _fake_popen_capturant(monkeypatch, captured)

    _run_one_shot_command(
        ["acpx", "grok-build", "exec", "prompt"], timeout_s=12, agent="grok-build"
    )

    assert Path(captured["env"]["GROK_HOME"]) == grok_home


def test_agent_grok_avec_profil_invalide_refuse_avant_le_subprocess(monkeypatch, tmp_path) -> None:
    _profil_codex_valide(tmp_path, monkeypatch)
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "profil-inexistant"))

    def jamais_lance(*_args, **_kwargs):
        raise AssertionError("le subprocess ne doit pas démarrer sur un profil grok invalide")

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.subprocess.Popen", jamais_lance)

    with pytest.raises(RuntimeError, match="profil ACPX grok illisible"):
        _run_one_shot_command(
            ["acpx", "grok-build", "exec", "prompt"], timeout_s=12, agent="grok-build"
        )


def test_run_one_shot_ne_sonde_plus_les_ponts_codex_acp(monkeypatch) -> None:
    class FakePopen:
        pid = 4242
        returncode = 0

        def __init__(self, command, **kwargs):
            self.command = command

        def communicate(self, timeout=None):
            return "OK", ""

    def fail_if_called():
        raise AssertionError("le reaper cwd ne doit plus sonder les ponts codex-acp")

    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend.subprocess.Popen", FakePopen)
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._terminate_process_group", lambda _pid: None)
    monkeypatch.setattr("trader.infrastructure.llm.acpx_backend._codex_acp_pids", fail_if_called, raising=False)

    result = _run_one_shot_command(["acpx", "exec", "prompt"], timeout_s=12)

    assert result.returncode == 0
    assert result.stdout == "OK"
    assert result.stderr == ""


def test_openai_compatible_backend_appelle_chat_completions() -> None:
    calls = []

    def post_json(url: str, payload: dict, headers: dict, timeout_s: int) -> dict:
        calls.append((url, payload, headers, timeout_s))
        return {"choices": [{"message": {"content": '{"symbol":"SPY","action":"HOLD"}'}}]}

    backend = OpenAICompatibleBackend(
        provider="ollama-cloud",
        api_key="secret",
        base_url="https://ollama.com/v1",
        model="nemotron-3-nano:30b-cloud",
        post_json=post_json,
    )

    result = backend.complete("Décide en JSON", timeout_s=42)

    assert isinstance(result, LlmCompletion)
    assert result.text == '{"symbol":"SPY","action":"HOLD"}'
    assert result.provider == "ollama-cloud"
    assert calls == [
        (
            "https://ollama.com/v1/chat/completions",
            {
                "model": "nemotron-3-nano:30b-cloud",
                "messages": [{"role": "user", "content": "Décide en JSON"}],
                "temperature": 0,
            },
            {"Authorization": "Bearer secret", "Content-Type": "application/json"},
            42,
        )
    ]


def test_openai_compatible_backend_classe_429_retryable() -> None:
    def post_json(url: str, payload: dict, headers: dict, timeout_s: int) -> dict:
        raise RuntimeError(json.dumps({"status": 429, "body": "rate limit"}))

    backend = OpenAICompatibleBackend(
        provider="ollama-cloud",
        api_key="secret",
        base_url="https://ollama.com/v1",
        model="nemotron-3-nano:30b-cloud",
        post_json=post_json,
    )

    result = backend.complete("prompt", timeout_s=12)

    assert isinstance(result, LlmFailure)
    assert result.retryable is True
    assert result.code == "rate_limited"


def test_openai_compatible_backend_classe_abonnement_ollama() -> None:
    def post_json(url: str, payload: dict, headers: dict, timeout_s: int) -> dict:
        raise RuntimeError(
            json.dumps(
                {
                    "status": 403,
                    "body": '{"error":"this model requires a subscription, upgrade for access"}',
                }
            )
        )

    backend = OpenAICompatibleBackend(
        provider="ollama-cloud",
        api_key="secret",
        base_url="https://ollama.com/v1",
        model="glm-5.1:cloud",
        post_json=post_json,
    )

    result = backend.complete("prompt", timeout_s=12)

    assert isinstance(result, LlmFailure)
    assert result.retryable is False
    assert result.code == "subscription_required"
    assert "requires a subscription" in result.message


def test_build_acpx_session_new_command_cree_une_session_nommee() -> None:
    cmd = llm.build_acpx_session_new_command(
        "casys-trader:runtime-brain:0",
        acpx_bin="acpx",
        model="gpt-5.5/medium",
        timeout_s=30,
    )

    assert cmd == [
        "acpx",
        "--format",
        "quiet",
        "--allowed-tools",
        "",
        "--no-terminal",
        "--non-interactive-permissions",
        "deny",
        "--model",
        "gpt-5.5/medium",
        "--timeout",
        "30",
        "sessions",
        "new",
        "-s",
        "casys-trader:runtime-brain:0",
    ]


def test_build_acpx_session_prompt_command_envoie_un_prompt_dans_une_session_nommee() -> None:
    cmd = llm.build_acpx_session_prompt_command(
        "casys-trader:runtime-brain:0",
        "analyse ce symbole",
        acpx_bin="acpx",
        model="gpt-5.5/medium",
        timeout_s=45,
    )

    assert cmd == [
        "acpx",
        "--format",
        "quiet",
        "--allowed-tools",
        "",
        "--no-terminal",
        "--non-interactive-permissions",
        "deny",
        "--model",
        "gpt-5.5/medium",
        "--timeout",
        "45",
        "prompt",
        "-s",
        "casys-trader:runtime-brain:0",
        "analyse ce symbole",
    ]


def test_build_acpx_session_close_command_ferme_une_session_nommee() -> None:
    cmd = llm.build_acpx_session_close_command(
        "casys-trader:runtime-brain:0",
        acpx_bin="acpx",
    )

    assert cmd == [
        "acpx",
        "--format",
        "quiet",
        "--no-terminal",
        "--non-interactive-permissions",
        "deny",
        "sessions",
        "close",
        "casys-trader:runtime-brain:0",
    ]


def test_build_acpx_session_config_command_applique_une_option() -> None:
    cmd = llm.build_acpx_session_config_command(
        "casys-trader:runtime-brain:0",
        key="reasoning_effort",
        value="medium",
        acpx_bin="acpx",
    )

    assert cmd == [
        "acpx",
        "--format",
        "quiet",
        "--no-terminal",
        "--non-interactive-permissions",
        "deny",
        "set",
        "reasoning_effort",
        "medium",
        "--session",
        "casys-trader:runtime-brain:0",
    ]


def test_session_admin_commands_conservent_le_cwd_de_la_session_agent_exec(monkeypatch) -> None:
    monkeypatch.setenv("CASYS_AGENT_EXEC", "1")
    monkeypatch.setattr(
        "trader.infrastructure.llm.acpx_backend.agent_exec_scratch_dir",
        lambda _codex_home=None: "/tmp/casys-trader-test-scratch",
    )

    config_cmd = llm.build_acpx_session_config_command(
        "casys-trader:runtime-brain:0",
        key="reasoning_effort",
        value="medium",
        acpx_bin="acpx",
    )
    close_cmd = llm.build_acpx_session_close_command(
        "casys-trader:runtime-brain:0",
        acpx_bin="acpx",
    )

    assert config_cmd[3:5] == ["--cwd", "/tmp/casys-trader-test-scratch"]
    assert close_cmd[3:5] == ["--cwd", "/tmp/casys-trader-test-scratch"]


def test_build_acpx_session_commands_peuvent_cibler_un_agent_dedie() -> None:
    new_cmd = llm.build_acpx_session_new_command(
        "casys-trader:runtime-brain:0",
        acpx_bin="acpx",
        model="claude-sonnet",
        timeout_s=30,
        agent="claude",
    )
    prompt_cmd = llm.build_acpx_session_prompt_command(
        "casys-trader:runtime-brain:0",
        "analyse",
        acpx_bin="acpx",
        model="claude-sonnet",
        timeout_s=30,
        agent="claude",
    )
    close_cmd = llm.build_acpx_session_close_command(
        "casys-trader:runtime-brain:0",
        acpx_bin="acpx",
        agent="claude",
    )

    assert new_cmd[-5:] == ["claude", "sessions", "new", "-s", "casys-trader:runtime-brain:0"]
    assert prompt_cmd[-5:] == ["claude", "prompt", "-s", "casys-trader:runtime-brain:0", "analyse"]
    assert close_cmd[-4:] == ["claude", "sessions", "close", "casys-trader:runtime-brain:0"]


def test_build_acpx_command_peut_cibler_un_agent_dedie() -> None:
    cmd = build_acpx_command(
        "consolide ces learnings",
        acpx_bin="acpx",
        model="gpt-5.5/high",
        timeout_s=240,
        agent="codex",
    )

    assert cmd == [
        "acpx",
        "--format",
        "quiet",
        "--allowed-tools",
        "",
        "--no-terminal",
        "--non-interactive-permissions",
        "deny",
        "--model",
        "gpt-5.5/high",
        "--timeout",
        "240",
        "codex",
        "exec",
        "consolide ces learnings",
    ]


def test_build_acpx_command_peut_prefixer_un_label_identifiable() -> None:
    cmd = build_acpx_command(
        "prompt",
        acpx_bin="acpx",
        model="gpt-5.3-codex-spark/medium",
        timeout_s=12,
        session_label="casys-trader:runtime-brain",
    )

    assert cmd[-1].startswith("[casys-trader:runtime-brain]\n")
    assert cmd[-1].endswith("prompt")


def test_build_default_router_from_env_peut_nommer_lagent_acpx(monkeypatch) -> None:
    monkeypatch.delenv("TRADER_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("TRADER_ACPX_BIN", raising=False)

    router = build_default_router_from_env(
        env_path=None,
        acpx_bin="acpx-custom",
        spark_model="gpt-5.5/high",
        acpx_provider="consolidator",
        acpx_agent="codex",
    )

    backend = router.backends[0]
    assert isinstance(backend, AcpxBackend)
    assert backend.provider == "consolidator"
    assert backend.model == "gpt-5.5/high"
    assert backend.acpx_bin == "acpx-custom"
    assert backend.agent == "codex"
    assert backend.session_label == "casys-trader:learning-consolidator"


def test_build_default_router_from_env_labelle_le_brain_runtime(monkeypatch) -> None:
    monkeypatch.delenv("TRADER_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    router = build_default_router_from_env(env_path=None)

    backend = router.backends[0]
    assert isinstance(backend, AcpxBackend)
    assert backend.session_label == "casys-trader:runtime-brain"


def test_build_default_router_from_env_knobs_env_du_brain_trader(monkeypatch) -> None:
    """TRADER_ACPX_AGENT/TRADER_MODEL reconfigurent le profil trader par défaut."""
    monkeypatch.delenv("TRADER_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("TRADER_ACPX_AGENT", "kimi")
    monkeypatch.setenv("TRADER_MODEL", "kimi-code/kimi-for-coding")
    monkeypatch.setenv("TRADER_REASONING_EFFORT", "medium")

    router = build_default_router_from_env(env_path=None)

    backend = router.backends[0]
    assert isinstance(backend, AcpxBackend)
    assert backend.provider == "acpx"
    assert backend.agent == "kimi"
    assert backend.model == "kimi-code/kimi-for-coding"
    assert backend.reasoning_effort == "medium"


def test_build_default_router_from_env_choix_explicite_immune_aux_knobs_trader(monkeypatch) -> None:
    """Un modèle/agent explicite (ex. rotation, universe) n'est pas écrasé par les knobs."""
    monkeypatch.delenv("TRADER_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("TRADER_ACPX_AGENT", "kimi")
    monkeypatch.setenv("TRADER_MODEL", "kimi-code/kimi-for-coding")

    router = build_default_router_from_env(env_path=None, spark_model="gpt-5.6-sol")

    backend = router.backends[0]
    assert backend.agent is None
    assert backend.model == "gpt-5.6-sol"

    router = build_default_router_from_env(env_path=None, spark_model="gpt-5.5", acpx_agent="codex")

    backend = router.backends[0]
    assert backend.agent == "codex"
    assert backend.model == "gpt-5.5"


def test_build_default_router_from_env_knobs_trader_ne_fuitent_pas_vers_universe(monkeypatch) -> None:
    """Le provider universe garde ses knobs dédiés, sans hériter des knobs trader."""
    monkeypatch.delenv("TRADER_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("TRADER_ACPX_AGENT", "kimi")
    monkeypatch.setenv("TRADER_MODEL", "kimi-code/kimi-for-coding")

    router = build_default_router_from_env(
        env_path=None, acpx_provider="universe", spark_model="gpt-5.6-sol"
    )

    backend = router.backends[0]
    assert backend.provider == "universe"
    assert backend.agent is None
    assert backend.model == "gpt-5.6-sol"


def test_build_default_router_from_env_partage_le_profil_codex(monkeypatch) -> None:
    """Le preset actif partage un seul CODEX_HOME entre tous les rôles."""

    monkeypatch.delenv("TRADER_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("TRADER_CODEX_HOME", "/profiles/shared")
    monkeypatch.setenv("TRADER_UNIVERSE_CODEX_HOME", "/profiles/shared")

    brain = build_default_router_from_env(env_path=None).backends[0]
    universe = build_default_router_from_env(
        env_path=None, acpx_provider="universe", spark_model="gpt-5.6-terra"
    ).backends[0]

    assert brain.codex_home == "/profiles/shared"
    assert universe.codex_home == "/profiles/shared"


def test_build_default_router_from_env_profil_codex_absent_reste_global(monkeypatch) -> None:
    """Sans var par rôle : None → le transport retombe sur CODEX_HOME (historique)."""

    monkeypatch.delenv("TRADER_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    for name in ("TRADER_CODEX_HOME", "TRADER_UNIVERSE_CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)

    router = build_default_router_from_env(env_path=None)

    assert all(getattr(backend, "codex_home", None) is None for backend in router.backends)


def test_build_default_router_from_env_configure_acpx_puis_ollama(monkeypatch) -> None:
    monkeypatch.setenv("TRADER_OLLAMA_API_KEY", "secret")
    monkeypatch.setenv("TRADER_OLLAMA_MODEL", "nemotron-3-nano:30b-cloud")
    monkeypatch.delenv("TRADER_SPARK_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("TRADER_ACPX_AGENT", raising=False)
    monkeypatch.delenv("TRADER_MODEL", raising=False)

    router = build_default_router_from_env(env_path=None)

    assert [backend.provider for backend in router.backends] == ["acpx", "acpx-claude-sonnet", "ollama-cloud"]
    assert router.backends[0].model == "gpt-5.6-terra"
    assert router.backends[2].model == "nemotron-3-nano:30b-cloud"


def test_build_default_router_from_env_configure_ollama_dedie_au_consolidateur(monkeypatch) -> None:
    monkeypatch.setenv("TRADER_OLLAMA_API_KEY", "runtime-secret")
    monkeypatch.setenv("TRADER_OLLAMA_MODEL", "nemotron-3-nano:30b-cloud")
    monkeypatch.setenv("TRADER_CONSOLIDATOR_OLLAMA_API_KEY", "consolidator-secret")
    monkeypatch.setenv("TRADER_CONSOLIDATOR_OLLAMA_BASE_URL", "https://ollama.com/v1")
    monkeypatch.setenv("TRADER_CONSOLIDATOR_OLLAMA_MODEL", "kimi-k2:cloud")

    router = build_default_router_from_env(env_path=None, acpx_provider="consolidator")

    assert [backend.provider for backend in router.backends] == ["consolidator", "ollama-cloud"]
    assert router.backends[1].api_key == "consolidator-secret"
    assert router.backends[1].base_url == "https://ollama.com/v1"
    assert router.backends[1].model == "kimi-k2:cloud"


def test_build_default_router_from_env_charge_un_dotenv_local(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("TRADER_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("TRADER_OLLAMA_MODEL", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "TRADER_OLLAMA_API_KEY=secret-from-file\n"
        "TRADER_OLLAMA_BASE_URL=https://ollama.com/v1\n"
        "TRADER_OLLAMA_MODEL=nemotron-3-nano:30b-cloud\n"
    )

    monkeypatch.delenv("TRADER_SPARK_FALLBACK_MODEL", raising=False)
    router = build_default_router_from_env(env_path=env_path)

    assert [backend.provider for backend in router.backends] == ["acpx", "acpx-claude-sonnet", "ollama-cloud"]
    assert router.backends[2].model == "nemotron-3-nano:30b-cloud"


def test_trade_router_3_tiers_dans_lordre(monkeypatch) -> None:
    """Router de trade = acpx → acpx-claude-sonnet → ollama-cloud (3 tiers)."""
    monkeypatch.setenv("TRADER_OLLAMA_API_KEY", "secret")
    monkeypatch.delenv("TRADER_SPARK_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("TRADER_ACPX_AGENT", raising=False)
    monkeypatch.delenv("TRADER_MODEL", raising=False)

    router = build_default_router_from_env(env_path=None)

    providers = [b.provider for b in router.backends]
    assert providers == ["acpx", "acpx-claude-sonnet", "ollama-cloud"]
    assert router.backends[0].model == "gpt-5.6-terra"
    assert router.backends[1].model == "sonnet"
    assert router.backends[1].agent == "claude"
    assert router.backends[2].provider == "ollama-cloud"


def test_consolidator_router_sans_spark_fallback(monkeypatch) -> None:
    """Le router consolidateur ne contient PAS de tier acpx-claude-sonnet."""
    monkeypatch.setenv("TRADER_CONSOLIDATOR_OLLAMA_API_KEY", "secret")
    monkeypatch.delenv("TRADER_SPARK_FALLBACK_MODEL", raising=False)

    router = build_default_router_from_env(env_path=None, acpx_provider="consolidator")

    providers = [b.provider for b in router.backends]
    assert "acpx-claude-sonnet" not in providers
    assert providers == ["consolidator", "ollama-cloud"]


def test_env_override_spark_fallback_model_custom(monkeypatch) -> None:
    """TRADER_SPARK_FALLBACK_MODEL défini → tier 2 utilise ce modèle."""
    monkeypatch.setenv("TRADER_SPARK_FALLBACK_MODEL", "gpt-custom-spark")
    monkeypatch.delenv("TRADER_OLLAMA_API_KEY", raising=False)

    router = build_default_router_from_env(env_path=None)

    providers = [b.provider for b in router.backends]
    assert "acpx-claude-sonnet" in providers
    fallback = next(b for b in router.backends if b.provider == "acpx-claude-sonnet")
    assert fallback.model == "gpt-custom-spark"


def test_env_override_spark_fallback_vide_desactive_le_tier(monkeypatch) -> None:
    """TRADER_SPARK_FALLBACK_MODEL='' → pas de tier acpx-claude-sonnet (2 tiers seulement)."""
    monkeypatch.setenv("TRADER_SPARK_FALLBACK_MODEL", "")
    monkeypatch.setenv("TRADER_OLLAMA_API_KEY", "secret")

    router = build_default_router_from_env(env_path=None)

    providers = [b.provider for b in router.backends]
    assert "acpx-claude-sonnet" not in providers
    assert providers == ["acpx", "ollama-cloud"]


def test_trader_acpx_bin_prime_sur_le_parametre(monkeypatch) -> None:
    """TRADER_ACPX_BIN non-vide prime sur le paramètre acpx_bin."""
    monkeypatch.setenv("TRADER_ACPX_BIN", "/opt/fork-acpx/dist/cli.js")
    monkeypatch.delenv("TRADER_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("TRADER_SPARK_FALLBACK_MODEL", raising=False)

    router = build_default_router_from_env(env_path=None, acpx_bin="acpx")

    # Les deux backends acpx doivent utiliser le bin de l'env
    acpx_backends = [b for b in router.backends if isinstance(b, AcpxBackend)]
    assert all(b.acpx_bin == "/opt/fork-acpx/dist/cli.js" for b in acpx_backends)


def test_trader_acpx_bin_vide_noverride_pas(monkeypatch) -> None:
    """TRADER_ACPX_BIN vide → le paramètre acpx_bin est conservé."""
    monkeypatch.setenv("TRADER_ACPX_BIN", "")
    monkeypatch.delenv("TRADER_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("TRADER_SPARK_FALLBACK_MODEL", raising=False)

    router = build_default_router_from_env(env_path=None, acpx_bin="mon-acpx-custom")

    acpx_backends = [b for b in router.backends if isinstance(b, AcpxBackend)]
    assert all(b.acpx_bin == "mon-acpx-custom" for b in acpx_backends)


def test_trader_acpx_bin_nisolation_pas_le_consolidateur(monkeypatch) -> None:
    """Finding 3 — TRADER_ACPX_BIN ne doit PAS écraser le bin du consolidateur.

    Quand build_consolidator_router_from_env résout TRADER_CONSOLIDATOR_ACPX_BIN
    et le passe à build_default_router_from_env, TRADER_ACPX_BIN (global) ne doit
    pas l'écraser. Les deux binaires sont indépendants (.env.example §9 et §13).
    """
    monkeypatch.setenv("TRADER_ACPX_BIN", "/opt/trading/acpx")
    monkeypatch.setenv("TRADER_CONSOLIDATOR_ACPX_BIN", "/opt/review/acpx")
    monkeypatch.delenv("TRADER_CONSOLIDATOR_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("TRADER_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    # build_default_router_from_env avec le bin consolidateur pré-résolu
    router = build_default_router_from_env(
        env_path=None,
        acpx_bin="/opt/review/acpx",  # résolu en amont par build_consolidator_router_from_env
        acpx_provider="consolidator",
    )

    backend = router.backends[0]
    assert backend.acpx_bin == "/opt/review/acpx", (
        f"Le bin consolidateur ne doit pas être écrasé par TRADER_ACPX_BIN ; got {backend.acpx_bin!r}"
    )


def test_acpx_exec_off_par_defaut_garde_le_contrat_pur_texte(monkeypatch) -> None:
    """Sans CASYS_AGENT_EXEC : zéro outil natif, comportement historique inchangé."""
    monkeypatch.delenv("CASYS_AGENT_EXEC", raising=False)
    cmd = build_acpx_command("hi", acpx_bin="acpx", model="gpt-5.5", timeout_s=60)
    assert "--allowed-tools" in cmd
    assert cmd[cmd.index("--allowed-tools") + 1] == ""  # aucun outil
    assert "--non-interactive-permissions" in cmd
    assert "--approve-all" not in cmd
    assert "--cwd" not in cmd


def test_acpx_exec_on_active_les_outils_et_cage_le_cwd_sur_un_scratch(monkeypatch, tmp_path) -> None:
    """CASYS_AGENT_EXEC=1 + CODEX_HOME : outils natifs + cwd = scratch sous CODEX_HOME."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CASYS_AGENT_EXEC", "1")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("CASYS_AGENT_EXEC_CWD", raising=False)
    cmd = build_acpx_command("hi", acpx_bin="acpx", model="gpt-5.5", timeout_s=60)
    assert "--allowed-tools" not in cmd  # outils natifs (exec/python) disponibles
    assert "--approve-all" in cmd
    scratch = cmd[cmd.index("--cwd") + 1]
    assert scratch == str(codex_home / "calc-scratch")  # sous CODEX_HOME, pas le repo
    assert (codex_home / "calc-scratch").is_dir()  # créé par agent_exec_scratch_dir()
    runtime_instructions = (codex_home / "calc-scratch" / "AGENTS.md").read_text()
    assert "not repository incident investigations" in runtime_instructions
    assert "Do not inspect filesystem content" in runtime_instructions
    assert "deterministic numerical calculations" in runtime_instructions
    assert "pure JSON object" in runtime_instructions


def test_acpx_exec_on_sans_codex_home_fail_close(monkeypatch) -> None:
    """Sans CODEX_HOME, l'exec serait NON confiné (~/.codex danger-full-access) → refus."""
    monkeypatch.setenv("CASYS_AGENT_EXEC", "1")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    with pytest.raises(RuntimeError, match="CODEX_HOME"):
        build_acpx_command("hi", acpx_bin="acpx", model="gpt-5.5", timeout_s=60)


def test_acpx_exec_cwd_hors_codex_home_est_refuse(monkeypatch, tmp_path) -> None:
    """Un CASYS_AGENT_EXEC_CWD hors CODEX_HOME (ex: le repo) est rejeté."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CASYS_AGENT_EXEC", "1")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CASYS_AGENT_EXEC_CWD", str(tmp_path / "elsewhere"))
    with pytest.raises(RuntimeError, match="CODEX_HOME"):
        build_acpx_command("hi", acpx_bin="acpx", model="gpt-5.5", timeout_s=60)
