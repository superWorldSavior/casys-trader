import json
import signal
import subprocess

import trader.agent.llm as llm
from trader.agent.llm import (
    AcpxBackend,
    LlmCompletion,
    LlmFailure,
    LlmRouter,
    OpenAICompatibleBackend,
    build_acpx_command,
    build_default_router_from_env,
    _codex_acp_pids,
    _looks_retryable_provider_error,
    _reap_orphan_bridges,
    _run_one_shot_command,
)


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


def test_acpx_backend_timeout_est_un_echec_non_retryable(monkeypatch) -> None:
    monkeypatch.setattr("trader.agent.llm.shutil.which", lambda _bin: "/usr/local/bin/acpx")
    monkeypatch.setattr("trader.agent.llm._terminate_process_group", lambda _pid: None)
    monkeypatch.setattr("trader.agent.llm._codex_acp_pids", lambda: set())

    class TimeoutPopen:
        pid = 4242
        returncode = None

        def __init__(self, command, **kwargs):
            self.command = command

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd=self.command, timeout=timeout)

        def kill(self):
            return None

    monkeypatch.setattr("trader.agent.llm.subprocess.Popen", TimeoutPopen)

    result = AcpxBackend().complete("prompt", timeout_s=900)

    assert isinstance(result, LlmFailure)
    assert result.provider == "acpx"
    assert result.code == "timeout"
    assert result.retryable is False


def test_acpx_backend_consolidateur_traite_internal_error_comme_retryable(monkeypatch) -> None:
    monkeypatch.setattr("trader.agent.llm.shutil.which", lambda _bin: "/usr/local/bin/acpx")
    monkeypatch.setattr("trader.agent.llm._terminate_process_group", lambda _pid: None)
    monkeypatch.setattr("trader.agent.llm._codex_acp_pids", lambda: set())

    class InternalErrorPopen:
        pid = 4242
        returncode = 1

        def __init__(self, command, **kwargs):
            self.command = command

        def communicate(self, timeout=None):
            return "", "Internal error\n"

    monkeypatch.setattr("trader.agent.llm.subprocess.Popen", InternalErrorPopen)

    result = AcpxBackend(provider="consolidator", model="gpt-5.5/high").complete("prompt", timeout_s=240)

    assert isinstance(result, LlmFailure)
    assert result.retryable is True
    assert result.code == "provider_error"


def test_acpx_backend_runtime_traite_internal_error_comme_retryable(monkeypatch) -> None:
    monkeypatch.setattr("trader.agent.llm.shutil.which", lambda _bin: "/usr/local/bin/acpx")
    monkeypatch.setattr("trader.agent.llm._terminate_process_group", lambda _pid: None)
    monkeypatch.setattr("trader.agent.llm._codex_acp_pids", lambda: set())

    class InternalErrorPopen:
        pid = 4242
        returncode = 1

        def __init__(self, command, **kwargs):
            self.command = command

        def communicate(self, timeout=None):
            return "", "Internal error\n"

    monkeypatch.setattr("trader.agent.llm.subprocess.Popen", InternalErrorPopen)

    result = AcpxBackend(provider="acpx", model="gpt-5.5").complete("prompt", timeout_s=240)

    assert isinstance(result, LlmFailure)
    assert result.retryable is True
    assert result.code == "provider_error"


def test_acpx_backend_exit_non_zero_sans_sortie_est_retryable(monkeypatch) -> None:
    """exit!=0 acpx SANS aucune sortie (stderr+stdout vides) = blip provider
    transitoire (quota/dispo qui hoquette), donc retryable -> le routeur peut
    tomber en fallback au lieu de figer un HOLD sec."""
    monkeypatch.setattr("trader.agent.llm.shutil.which", lambda _bin: "/usr/local/bin/acpx")
    monkeypatch.setattr("trader.agent.llm._terminate_process_group", lambda _pid: None)
    monkeypatch.setattr("trader.agent.llm._codex_acp_pids", lambda: set())

    class EmptyExitPopen:
        pid = 4242
        returncode = 1

        def __init__(self, command, **kwargs):
            self.command = command

        def communicate(self, timeout=None):
            return "", ""

    monkeypatch.setattr("trader.agent.llm.subprocess.Popen", EmptyExitPopen)

    result = AcpxBackend(provider="acpx", model="gpt-5.5").complete("prompt", timeout_s=240)

    assert isinstance(result, LlmFailure)
    assert result.retryable is True
    assert result.code == "provider_error"


def test_acpx_backend_exit_non_zero_avec_erreur_explicite_reste_non_retryable(monkeypatch) -> None:
    """Garde-fou : un exit!=0 avec un message d'erreur explicite et NON transitoire
    (ni rate-limit, ni internal-error, ni sortie vide) reste un nonzero_exit
    non-retryable -- on ne doit pas tout rendre retryable."""
    monkeypatch.setattr("trader.agent.llm.shutil.which", lambda _bin: "/usr/local/bin/acpx")
    monkeypatch.setattr("trader.agent.llm._terminate_process_group", lambda _pid: None)
    monkeypatch.setattr("trader.agent.llm._codex_acp_pids", lambda: set())

    class ExplicitErrorPopen:
        pid = 4242
        returncode = 1

        def __init__(self, command, **kwargs):
            self.command = command

        def communicate(self, timeout=None):
            return "", "fatal: configuration invalide\n"

    monkeypatch.setattr("trader.agent.llm.subprocess.Popen", ExplicitErrorPopen)

    result = AcpxBackend(provider="acpx", model="gpt-5.5").complete("prompt", timeout_s=240)

    assert isinstance(result, LlmFailure)
    assert result.retryable is False
    assert result.code == "nonzero_exit"


def test_acpx_backend_isole_et_nettoie_le_process_group(monkeypatch) -> None:
    monkeypatch.setattr("trader.agent.llm.shutil.which", lambda _bin: "/usr/local/bin/acpx")
    popen_calls = []
    cleaned_pids = []
    pid_snapshots = iter([set(), set()])

    class FakePopen:
        pid = 4242
        returncode = 0

        def __init__(self, command, **kwargs):
            popen_calls.append((command, kwargs))

        def communicate(self, timeout=None):
            return "OK", ""

    monkeypatch.setattr("trader.agent.llm.subprocess.Popen", FakePopen)
    monkeypatch.setattr("trader.agent.llm._terminate_process_group", lambda pid: cleaned_pids.append(pid))
    monkeypatch.setattr("trader.agent.llm._codex_acp_pids", lambda: next(pid_snapshots))

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

    monkeypatch.setattr("trader.agent.llm.subprocess.Popen", FakePopen)
    monkeypatch.setattr("trader.agent.llm._terminate_process_group", lambda _pid: None)
    monkeypatch.setattr("trader.agent.llm._codex_acp_pids", lambda: set())

    result = _run_one_shot_command(["acpx", "exec", "prompt"], timeout_s=12)

    assert result.returncode == 0
    env = captured["env"]
    assert "MallocStackLogging" not in env
    assert "MallocStackLoggingNoCompact" not in env
    assert env["TRADER_OLLAMA_MODEL"] == "nemotron-3-ultra:cloud"
    assert env["PATH"] == "/opt/homebrew/bin:/usr/bin"


def test_codex_acp_pids_filtre_sur_lexecutable_exact(monkeypatch) -> None:
    calls = []

    class FakeCompleted:
        returncode = 0
        stdout = "\n".join(
            [
                "101 /usr/bin/python worker.py codex-acp",
                "102 /opt/tools/codex-acp --stdio",
                "103 codex-acp --stdio",
                "104 /tmp/not-codex-acp --stdio",
            ]
        )

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return FakeCompleted()

    monkeypatch.setattr("trader.agent.llm.subprocess.run", fake_run)

    assert _codex_acp_pids() == {102, 103}
    assert calls[0][0] == ["ps", "-axo", "pid=,command="]
    assert calls[0][1]["timeout"] > 0


def test_codex_acp_pids_timeout_retourne_vide(monkeypatch) -> None:
    def raise_timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs["timeout"])

    monkeypatch.setattr("trader.agent.llm.subprocess.run", raise_timeout)

    assert _codex_acp_pids() == set()


def test_process_cwd_lsof_parse_un_realpath_et_timeout(monkeypatch) -> None:
    calls = []

    class FakeCompleted:
        returncode = 0
        stdout = "p42\nn/tmp/repo-link\n"

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return FakeCompleted()

    monkeypatch.setattr("trader.agent.llm.subprocess.run", fake_run)
    monkeypatch.setattr("trader.agent.llm.os.path.realpath", lambda path: f"/real{path}")

    assert llm._process_cwd(42) == "/real/tmp/repo-link"
    assert calls[0][0] == ["lsof", "-a", "-d", "cwd", "-p", "42", "-Fn"]
    assert calls[0][1]["timeout"] > 0

    def raise_timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs["timeout"])

    monkeypatch.setattr("trader.agent.llm.subprocess.run", raise_timeout)

    assert llm._process_cwd(42) is None


def test_reap_orphan_bridges_filtre_le_diff_par_cwd_exact(monkeypatch) -> None:
    killed = []
    cwd_by_pid = {
        2: "/repo",
        3: "/other-repo",
        4: None,
    }

    monkeypatch.setattr("trader.agent.llm._codex_acp_pids", lambda: {1, 2, 3, 4})
    monkeypatch.setattr("trader.agent.llm._process_cwd", lambda pid: cwd_by_pid[pid])
    monkeypatch.setattr("trader.agent.llm.os.kill", lambda pid, sig: killed.append((pid, sig)))

    _reap_orphan_bridges({1}, "/repo")

    assert killed == [(2, signal.SIGKILL)]


def test_reap_orphan_bridges_exige_une_egalite_cwd_exacte(monkeypatch) -> None:
    killed = []

    monkeypatch.setattr("trader.agent.llm._codex_acp_pids", lambda: {2})
    monkeypatch.setattr("trader.agent.llm._process_cwd", lambda _pid: "/repo/worktrees/x")
    monkeypatch.setattr("trader.agent.llm.os.kill", lambda pid, sig: killed.append((pid, sig)))

    _reap_orphan_bridges(set(), "/repo")

    assert killed == []


def test_run_one_shot_reap_uniquement_les_nouveaux_ponts_codex_acp(monkeypatch) -> None:
    pid_snapshots = iter([{1, 2}, {1, 2, 3, 4}])
    cleaned_pids = []
    killed = []

    class FakePopen:
        pid = 4242
        returncode = 7

        def __init__(self, command, **kwargs):
            self.command = command

        def communicate(self, timeout=None):
            return "STDOUT", "STDERR"

    monkeypatch.setattr("trader.agent.llm.subprocess.Popen", FakePopen)
    monkeypatch.setattr("trader.agent.llm._terminate_process_group", lambda pid: cleaned_pids.append(pid))
    monkeypatch.setattr("trader.agent.llm._codex_acp_pids", lambda: next(pid_snapshots))
    monkeypatch.setattr("trader.agent.llm._process_cwd", lambda _pid: "/repo")
    monkeypatch.setattr("trader.agent.llm.os.getcwd", lambda: "/repo")
    monkeypatch.setattr("trader.agent.llm.os.kill", lambda pid, sig: killed.append((pid, sig)))

    result = _run_one_shot_command(["acpx", "exec", "prompt"], timeout_s=12)

    assert result.returncode == 7
    assert result.stdout == "STDOUT"
    assert result.stderr == "STDERR"
    assert cleaned_pids == [4242]
    assert {pid for pid, _sig in killed} == {3, 4}
    assert all(sig == signal.SIGKILL for _pid, sig in killed)


def test_run_one_shot_ne_tue_aucun_pont_sans_nouveau_pid(monkeypatch) -> None:
    pid_snapshots = iter([{1, 2}, {1, 2}])
    killed = []

    class FakePopen:
        pid = 4242
        returncode = 0

        def __init__(self, command, **kwargs):
            self.command = command

        def communicate(self, timeout=None):
            return "OK", ""

    monkeypatch.setattr("trader.agent.llm.subprocess.Popen", FakePopen)
    monkeypatch.setattr("trader.agent.llm._terminate_process_group", lambda _pid: None)
    monkeypatch.setattr("trader.agent.llm._codex_acp_pids", lambda: next(pid_snapshots))
    monkeypatch.setattr("trader.agent.llm._process_cwd", lambda _pid: "/repo")
    monkeypatch.setattr("trader.agent.llm.os.getcwd", lambda: "/repo")
    monkeypatch.setattr("trader.agent.llm.os.kill", lambda pid, sig: killed.append((pid, sig)))

    result = _run_one_shot_command(["acpx", "exec", "prompt"], timeout_s=12)

    assert result.returncode == 0
    assert result.stdout == "OK"
    assert result.stderr == ""
    assert killed == []


def test_run_one_shot_ignore_les_erreurs_de_reap_des_ponts(monkeypatch) -> None:
    pid_snapshots = iter([{1}, {1, 2}])
    kill_attempts = []

    class FakePopen:
        pid = 4242
        returncode = 0

        def __init__(self, command, **kwargs):
            self.command = command

        def communicate(self, timeout=None):
            return "OK", "WARN"

    def raise_process_lookup(pid: int, sig: int) -> None:
        kill_attempts.append((pid, sig))
        raise ProcessLookupError

    monkeypatch.setattr("trader.agent.llm.subprocess.Popen", FakePopen)
    monkeypatch.setattr("trader.agent.llm._terminate_process_group", lambda _pid: None)
    monkeypatch.setattr("trader.agent.llm._codex_acp_pids", lambda: next(pid_snapshots))
    monkeypatch.setattr("trader.agent.llm._process_cwd", lambda _pid: "/repo")
    monkeypatch.setattr("trader.agent.llm.os.getcwd", lambda: "/repo")
    monkeypatch.setattr("trader.agent.llm.os.kill", raise_process_lookup)

    result = _run_one_shot_command(["acpx", "exec", "prompt"], timeout_s=12)

    assert result.returncode == 0
    assert result.stdout == "OK"
    assert result.stderr == "WARN"
    assert kill_attempts == [(2, signal.SIGKILL)]


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


def test_build_default_router_from_env_configure_acpx_puis_ollama(monkeypatch) -> None:
    monkeypatch.setenv("TRADER_OLLAMA_API_KEY", "secret")
    monkeypatch.setenv("TRADER_OLLAMA_MODEL", "nemotron-3-nano:30b-cloud")
    monkeypatch.delenv("TRADER_SPARK_FALLBACK_MODEL", raising=False)

    router = build_default_router_from_env()

    assert [backend.provider for backend in router.backends] == ["acpx", "acpx-claude-sonnet", "ollama-cloud"]
    assert router.backends[0].model == "gpt-5.5"
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

    router = build_default_router_from_env(env_path=None)

    providers = [b.provider for b in router.backends]
    assert providers == ["acpx", "acpx-claude-sonnet", "ollama-cloud"]
    assert router.backends[0].model == "gpt-5.5"
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
