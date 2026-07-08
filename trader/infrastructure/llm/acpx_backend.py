"""acpx subprocess/session transport for the LLM port."""

from __future__ import annotations

import logging
import os
import signal
import shutil
import subprocess
import time
from dataclasses import dataclass, is_dataclass, replace
from typing import Callable

from trader.domain.llm import LlmCompletion, LlmFailure
from trader.infrastructure.llm._errors import _failure_code_from_text, _looks_retryable_provider_error
from trader.support.system.process_env import sanitized_runtime_env

log = logging.getLogger(__name__)

# Defaults duplicated here intentionally: the adapter must be directly importable
# without asking the agent facade to import infrastructure while it is still loading.
_DEFAULT_ACPX_MODEL = "gpt-5.5"
_DEFAULT_RUNTIME_SESSION_LABEL = "casys-trader:runtime-brain"

# Plafond par-appel du subprocess acpx, DÉCOUPLÉ du budget-décision (lease).
# Un appel LLM normal fait 30-90s ; un tour figé (provider muet après
# task_started, cf incident AMCR 2026-07-06) resterait pendu jusqu'au budget
# total. Ce cap coupe l'appel individuel bien avant, sans toucher au lease.
_DEFAULT_PER_CALL_TIMEOUT_CAP_S = 150


def _per_call_timeout_cap_s() -> int:
    """Cap par-appel subprocess acpx (secondes), env ``CASYS_ACPX_CALL_TIMEOUT_S``."""
    raw = os.getenv("CASYS_ACPX_CALL_TIMEOUT_S")
    if raw is None:
        return _DEFAULT_PER_CALL_TIMEOUT_CAP_S
    try:
        return max(int(raw), 1)
    except ValueError:
        return _DEFAULT_PER_CALL_TIMEOUT_CAP_S


def _log_acpx_call(
    *, session, symbol, pid, provider, timeout_s: int, dur_s: float, outcome: str
) -> None:
    """Journal de vie d'un appel acpx : 1 ligne structurée par subprocess.

    Champs : session (=task_id:attempt) · symbol · pid · timeout appliqué ·
    durée réelle · issue (ok|timeout|nonzero|nonzero_retryable|error|unavailable).
    Corrélable au ledger par ``session`` (préfixe = task_id).
    """
    log.info(
        "[acpx_call] session=%s symbol=%s pid=%s provider=%s timeout_s=%s dur_s=%.1f outcome=%s",
        session or "-",
        symbol or "-",
        pid if pid is not None else "-",
        provider,
        timeout_s,
        dur_s,
        outcome,
    )


def _acpx_global_flags(acpx_bin: str, *, model: str, timeout_s: int) -> list[str]:
    return [
        acpx_bin,
        "--format",
        "quiet",
        "--allowed-tools",
        "",
        "--no-terminal",
        "--non-interactive-permissions",
        "deny",
        "--model",
        model,
        "--timeout",
        str(timeout_s),
    ]


def _acpx_agent_part(agent: str | None) -> list[str]:
    return [] if not agent or agent == "default" else [agent]


def build_acpx_session_new_command(
    name: str,
    *,
    acpx_bin: str,
    model: str,
    timeout_s: int,
    agent: str | None = None,
) -> list[str]:
    return [
        *_acpx_global_flags(acpx_bin, model=model, timeout_s=timeout_s),
        *_acpx_agent_part(agent),
        "sessions",
        "new",
        "-s",
        name,
    ]


def build_acpx_session_prompt_command(
    name: str,
    prompt: str,
    *,
    acpx_bin: str,
    model: str,
    timeout_s: int,
    agent: str | None = None,
) -> list[str]:
    return [
        *_acpx_global_flags(acpx_bin, model=model, timeout_s=timeout_s),
        *_acpx_agent_part(agent),
        "prompt",
        "-s",
        name,
        prompt,
    ]


def build_acpx_session_close_command(
    name: str,
    *,
    acpx_bin: str,
    agent: str | None = None,
) -> list[str]:
    return [
        acpx_bin,
        "--format",
        "quiet",
        "--no-terminal",
        "--non-interactive-permissions",
        "deny",
        *_acpx_agent_part(agent),
        "sessions",
        "close",
        name,
    ]


def build_acpx_command(
    prompt: str,
    *,
    acpx_bin: str,
    model: str,
    timeout_s: int,
    agent: str | None = None,
    session_label: str | None = None,
) -> list[str]:
    labeled_prompt = _label_prompt(prompt, session_label=session_label)
    return [
        *_acpx_global_flags(acpx_bin, model=model, timeout_s=timeout_s),
        *_acpx_agent_part(agent),
        "exec",
        labeled_prompt,
    ]


def _clean_optional(value: str | None) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _label_prompt(prompt: str, *, session_label: str | None) -> str:
    cleaned = _clean_optional(session_label)
    if cleaned is None:
        return prompt
    return f"[{cleaned}]\n{prompt}"


def _looks_retryable_acpx_error(*, provider: str, text: str) -> bool:
    if _looks_retryable_provider_error(text):
        return True
    if "internal error" in text.lower():
        return True
    # exit!=0 sans aucune sortie (stderr+stdout vides) = échec provider
    # transitoire (blip quota/dispo) qu'acpx remonte muet sous --format quiet ;
    # on le rend retryable pour autoriser le fallback plutôt qu'un HOLD sec.
    if not text.strip():
        return True
    return False


def _terminate_process_group(pgid: int, *, grace_s: float = 2.0) -> None:
    if os.name != "posix":
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return

    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def _run_one_shot_command(
    command: list[str], *, timeout_s: int, on_pid: Callable[[int], None] | None = None
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name == "posix",
        env=sanitized_runtime_env(),
    )
    if on_pid is not None:
        on_pid(proc.pid)
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc.pid)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        _terminate_process_group(proc.pid)

    return subprocess.CompletedProcess(
        args=command,
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _run_and_parse(
    command: list[str],
    *,
    provider: str,
    model: str,
    acpx_bin: str,
    timeout_s: int,
    call_ctx: dict | None = None,
) -> LlmCompletion | LlmFailure:
    ctx = call_ctx or {}
    session = ctx.get("session")
    symbol = ctx.get("symbol")
    # Budget par-appel = min(budget demandé, cap transport). Le cap coupe un
    # appel figé sans attendre le budget-décision total (fenêtre opaque).
    budget_s = min(int(timeout_s), _per_call_timeout_cap_s())

    if shutil.which(acpx_bin) is None:
        _log_acpx_call(
            session=session,
            symbol=symbol,
            pid=None,
            provider=provider,
            timeout_s=budget_s,
            dur_s=0.0,
            outcome="unavailable",
        )
        return LlmFailure(
            provider=provider,
            model=model,
            code="acpx_unavailable",
            message=f"binaire '{acpx_bin}' introuvable",
            retryable=True,
        )

    pid_holder: dict[str, int | None] = {"pid": None}
    started = time.monotonic()
    try:
        proc = _run_one_shot_command(
            command,
            timeout_s=budget_s + 15,
            on_pid=lambda p: pid_holder.__setitem__("pid", p),
        )
    except subprocess.TimeoutExpired:
        _log_acpx_call(
            session=session,
            symbol=symbol,
            pid=pid_holder["pid"],
            provider=provider,
            timeout_s=budget_s,
            dur_s=time.monotonic() - started,
            outcome="timeout",
        )
        return LlmFailure(
            provider=provider,
            model=model,
            code="timeout",
            message=f"> {budget_s}s",
            # Un tour figé (provider muet) est TRANSITOIRE : rejouer. L'incident
            # AMCR 2026-07-06 s'est résolu au retry post-restart.
            retryable=True,
        )
    except Exception as exc:  # noqa: BLE001 - frontière fournisseur
        message = str(exc)
        _log_acpx_call(
            session=session,
            symbol=symbol,
            pid=pid_holder["pid"],
            provider=provider,
            timeout_s=budget_s,
            dur_s=time.monotonic() - started,
            outcome="error",
        )
        return LlmFailure(
            provider=provider,
            model=model,
            code=_failure_code_from_text(message),
            message=message,
            retryable=_looks_retryable_provider_error(message),
        )

    dur_s = time.monotonic() - started
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "")[:500]
        retryable = _looks_retryable_acpx_error(provider=provider, text=message)
        _log_acpx_call(
            session=session,
            symbol=symbol,
            pid=pid_holder["pid"],
            provider=provider,
            timeout_s=budget_s,
            dur_s=dur_s,
            outcome="nonzero_retryable" if retryable else "nonzero",
        )
        return LlmFailure(
            provider=provider,
            model=model,
            code=_failure_code_from_text(message) if retryable else "nonzero_exit",
            message=f"exit={proc.returncode} {message}",
            retryable=retryable,
        )

    _log_acpx_call(
        session=session,
        symbol=symbol,
        pid=pid_holder["pid"],
        provider=provider,
        timeout_s=budget_s,
        dur_s=dur_s,
        outcome="ok",
    )
    return LlmCompletion(provider=provider, model=model, text=proc.stdout)


@dataclass(frozen=True)
class AcpxSession:
    provider: str
    model: str
    acpx_bin: str
    name: str
    agent: str | None = None

    def send(
        self, prompt: str, *, timeout_s: int, call_ctx: dict | None = None
    ) -> LlmCompletion | LlmFailure:
        return _run_and_parse(
            build_acpx_session_prompt_command(
                self.name,
                prompt,
                acpx_bin=self.acpx_bin,
                model=self.model,
                timeout_s=timeout_s,
                agent=self.agent,
            ),
            provider=self.provider,
            model=self.model,
            acpx_bin=self.acpx_bin,
            timeout_s=timeout_s,
            call_ctx={**(call_ctx or {}), "session": self.name},
        )

    def close(self) -> None:
        try:
            _run_one_shot_command(
                build_acpx_session_close_command(
                    self.name,
                    acpx_bin=self.acpx_bin,
                    agent=self.agent,
                ),
                timeout_s=15,
            )
        except Exception as exc:  # noqa: BLE001 - fermeture best-effort, jamais bloquante
            log.warning("[acpx_session] close failed name=%s: %s", self.name, exc)
            return


class SessionProviderDown(Exception):
    def __init__(self, failure: LlmFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def session_complete_fn(session, call_ctx: dict | None = None):
    def _complete(prompt: str, timeout_s: int) -> LlmCompletion | LlmFailure:
        result = session.send(prompt, timeout_s=timeout_s, call_ctx=call_ctx)
        if isinstance(result, LlmFailure) and result.retryable:
            raise SessionProviderDown(result)
        return result

    return _complete


def run_with_session_fallback(
    backends,
    *,
    task_id: str,
    resolve,
    open_timeout_s: int,
) -> object | LlmFailure:
    last_failure: LlmFailure | None = None
    for attempt, backend in enumerate(backends):
        session = backend.open_session(f"{task_id}:{attempt}", timeout_s=open_timeout_s)
        if isinstance(session, LlmFailure):
            last_failure = session
            continue
        try:
            result = resolve(session)
            if (
                attempt > 0
                and last_failure is not None
                and is_dataclass(result)
                and hasattr(result, "llm_fallback_reason")
                and getattr(result, "llm_fallback_reason") is None
            ):
                result = replace(
                    result,
                    llm_fallback_reason=f"{last_failure.provider}:{last_failure.code}",
                )
            return result
        except SessionProviderDown as exc:
            last_failure = exc.failure
            continue
        finally:
            session.close()
    return last_failure or LlmFailure(
        provider="none",
        model="none",
        code="no_backend",
        message="aucun backend LLM configuré",
        retryable=False,
    )


@dataclass(frozen=True)
class AcpxBackend:
    provider: str = "acpx"
    model: str = _DEFAULT_ACPX_MODEL
    acpx_bin: str = "acpx"
    agent: str | None = None
    session_label: str | None = _DEFAULT_RUNTIME_SESSION_LABEL

    def open_session(self, name: str, *, timeout_s: int) -> AcpxSession | LlmFailure:
        res = _run_and_parse(
            build_acpx_session_new_command(
                name,
                acpx_bin=self.acpx_bin,
                model=self.model,
                timeout_s=timeout_s,
                agent=self.agent,
            ),
            provider=self.provider,
            model=self.model,
            acpx_bin=self.acpx_bin,
            timeout_s=timeout_s,
            call_ctx={"session": name},
        )
        if not isinstance(res, LlmCompletion):
            return res

        return AcpxSession(
            provider=self.provider,
            model=self.model,
            acpx_bin=self.acpx_bin,
            name=name,
            agent=self.agent,
        )

    def complete(self, prompt: str, *, timeout_s: int) -> LlmCompletion | LlmFailure:
        return _run_and_parse(
            build_acpx_command(
                prompt,
                acpx_bin=self.acpx_bin,
                model=self.model,
                timeout_s=timeout_s,
                agent=self.agent,
                session_label=self.session_label,
            ),
            provider=self.provider,
            model=self.model,
            acpx_bin=self.acpx_bin,
            timeout_s=timeout_s,
            call_ctx={"session": self.session_label},
        )


__all__ = [
    "AcpxBackend",
    "AcpxSession",
    "SessionProviderDown",
    "build_acpx_command",
    "build_acpx_session_close_command",
    "build_acpx_session_new_command",
    "build_acpx_session_prompt_command",
    "run_with_session_fallback",
    "session_complete_fn",
    "_per_call_timeout_cap_s",
    "_run_and_parse",
    "_run_one_shot_command",
    "_terminate_process_group",
]
