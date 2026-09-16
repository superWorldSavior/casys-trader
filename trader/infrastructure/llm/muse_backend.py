"""Native, one-shot Muse transport for offline model benches.

``muse exec`` runs the Meta provider without ACP: the model id served is
reported in the ``run.model.configured`` JSONL event, which this adapter
verifies on every call. Each completion runs in a disposable neutral cwd
(untrusted workspace: no repo rules, no skills) with writes and shell
disabled and a single model step, so a bench case stays a pure
``prompt -> texte`` completion comparable to the acpx one-shot runs.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable

from trader.domain.llm import LlmCompletion, LlmExecutionCapability, LlmFailure
from trader.infrastructure.llm._errors import _failure_code_from_text, _looks_retryable_provider_error
from trader.infrastructure.llm._subprocess import per_call_timeout_cap_s, terminate_process_group
from trader.support.system.process_env import sanitized_runtime_env

log = logging.getLogger(__name__)

DEFAULT_MUSE_BIN = "muse"
DEFAULT_MUSE_MODEL = "muse-spark-1.3"
MUSE_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
DEFAULT_MUSE_EFFORT = "high"
MUSE_RUNTIME_PATH = os.defpath
_ACTIVE_MUSE_PROCESS_GROUPS: set[int] = set()
_ACTIVE_MUSE_PROCESS_GROUPS_LOCK = threading.Lock()


def _register_muse_process_group(pgid: int) -> None:
    with _ACTIVE_MUSE_PROCESS_GROUPS_LOCK:
        _ACTIVE_MUSE_PROCESS_GROUPS.add(pgid)


def _unregister_muse_process_group(pgid: int) -> None:
    with _ACTIVE_MUSE_PROCESS_GROUPS_LOCK:
        _ACTIVE_MUSE_PROCESS_GROUPS.discard(pgid)


def _terminate_active_muse_process_groups() -> None:
    """Last-resort cleanup when bench callers outlive the main shutdown."""

    with _ACTIVE_MUSE_PROCESS_GROUPS_LOCK:
        groups = tuple(_ACTIVE_MUSE_PROCESS_GROUPS)
        _ACTIVE_MUSE_PROCESS_GROUPS.clear()
    for pgid in groups:
        terminate_process_group(pgid)


atexit.register(_terminate_active_muse_process_groups)


def parse_muse_model_spec(spec: str) -> tuple[str, str]:
    """Split a bench model spec ``<model-id>[/effort]`` (effort = ``high`` par défaut)."""

    raw = str(spec or "").strip()
    model_id, _, effort = raw.partition("/")
    model_id = model_id.strip()
    effort = effort.strip().lower() or DEFAULT_MUSE_EFFORT
    if not model_id:
        raise ValueError(f"modèle muse manquant dans {spec!r}")
    if effort not in MUSE_EFFORTS:
        raise ValueError(f"effort muse {effort!r} invalide, attendu parmi {list(MUSE_EFFORTS)!r}")
    return model_id, effort


def build_muse_command(
    prompt_file: str,
    *,
    muse_bin: str = DEFAULT_MUSE_BIN,
    muse_model: str = DEFAULT_MUSE_MODEL,
    reasoning_effort: str = DEFAULT_MUSE_EFFORT,
) -> list[str]:
    """Build the only allowed Muse invocation for bench completions.

    One model step, no session log, no foreign personal context, no writes,
    no shell: the prompt file is the only input and the JSONL stdout the
    only output. It deliberately never accepts approval, yolo, workspace
    trust, image or worktree flags.
    """

    return [
        muse_bin,
        "exec",
        "--json",
        "--model",
        muse_model,
        "--reasoning-effort",
        reasoning_effort,
        "--no-session-log",
        "--no-foreign-personal-context",
        "--disable-write",
        "--disable-shell",
        "--max-model-steps",
        "1",
        "--prompt-file",
        prompt_file,
    ]


def _muse_child_env() -> dict[str, str]:
    """Build the least-privilege environment for one Muse bench call.

    HOME stays the operator's: the Meta provider login is bound to that
    account. A system-only PATH prevents personal launchers from leaking
    into the run; the resolved absolute binary needs no PATH lookup.
    """

    child_env = dict(os.environ)
    child_env["PATH"] = MUSE_RUNTIME_PATH
    return sanitized_runtime_env(child_env)


def _run_muse_command(
    command: list[str],
    *,
    timeout_s: int,
    cwd: str,
    on_pid: Callable[[int], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            start_new_session=os.name == "posix",
            env=_muse_child_env(),
        )
        _register_muse_process_group(proc.pid)
        if on_pid is not None:
            on_pid(proc.pid)
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        assert proc is not None
        terminate_process_group(proc.pid)
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
        if proc is not None:
            try:
                terminate_process_group(proc.pid)
            finally:
                _unregister_muse_process_group(proc.pid)

    return subprocess.CompletedProcess(
        args=command,
        returncode=proc.returncode if proc is not None else 1,
        stdout=stdout,
        stderr=stderr,
    )


def _parse_muse_jsonl(output: str, *, expected_model: str) -> str:
    """Extract final text and verify the model actually served.

    The ``run.model.configured`` event is not advisory metadata: a model
    mismatch is a fail-closed transport error, so a wrong default can never
    become a bench review. The ``run.terminal.completed`` text is the
    authoritative final answer; output deltas are only a fallback.
    """

    served_model: str | None = None
    deltas: list[str] = []
    terminal: str | None = None
    terminal_text: str | None = None
    terminal_reason: str | None = None
    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("sortie JSONL Muse invalide") from exc
        if not isinstance(event, dict):
            raise RuntimeError("événement JSONL Muse invalide")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if event.get("payload_type") == "run.model.configured":
            candidate = payload.get("model_id")
            served_model = str(candidate).strip() if candidate is not None else None
        elif event.get("payload_type") == "run.output.delta":
            candidate = payload.get("text")
            if isinstance(candidate, str):
                deltas.append(candidate)
        elif event.get("payload_type") == "run.terminal.completed":
            terminal = payload.get("terminal")
            candidate = payload.get("text")
            terminal_text = candidate if isinstance(candidate, str) else None
            reason = payload.get("reason")
            terminal_reason = str(reason) if reason is not None else None

    if served_model is None:
        raise RuntimeError("appel Muse refusé : modèle servi non annoncé")
    if served_model != expected_model:
        raise RuntimeError(f"appel Muse refusé : modèle servi {served_model!r}, attendu {expected_model!r}")
    if terminal != "completed":
        raise RuntimeError(f"appel Muse refusé : run {terminal or 'sans terminaison'!r} ({terminal_reason or 'sans raison'})")
    text = terminal_text if terminal_text else "".join(deltas)
    if not text:
        raise RuntimeError("appel Muse refusé : réponse vide")
    return text


def _muse_failure(*, provider: str, model: str, code: str, message: str, retryable: bool) -> LlmFailure:
    return LlmFailure(provider=provider, model=model, code=code, message=message, retryable=retryable)


def _run_muse_and_parse(
    command: list[str],
    *,
    provider: str,
    model: str,
    expected_model: str,
    muse_bin: str,
    timeout_s: int,
    cwd: str,
) -> LlmCompletion | LlmFailure:
    budget_s = min(int(timeout_s), per_call_timeout_cap_s())
    resolved_muse_bin = shutil.which(muse_bin)
    if resolved_muse_bin is None:
        return _muse_failure(
            provider=provider,
            model=model,
            code="muse_unavailable",
            message=f"binaire '{muse_bin}' introuvable",
            retryable=True,
        )
    command = [resolved_muse_bin, *command[1:]]
    pid_holder: dict[str, int | None] = {"pid": None}
    started = time.monotonic()
    try:
        proc = _run_muse_command(
            command,
            timeout_s=budget_s + 15,
            cwd=cwd,
            on_pid=lambda pid: pid_holder.__setitem__("pid", pid),
        )
    except subprocess.TimeoutExpired:
        return _muse_failure(
            provider=provider,
            model=model,
            code="timeout",
            message=f"> {budget_s}s",
            retryable=True,
        )
    except Exception as exc:  # noqa: BLE001 - provider boundary
        message = str(exc)
        return _muse_failure(
            provider=provider,
            model=model,
            code=_failure_code_from_text(message),
            message=message,
            retryable=_looks_retryable_provider_error(message),
        )

    duration_s = time.monotonic() - started
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "")[:500]
        log.warning(
            "[muse_call] pid=%s provider=%s timeout_s=%s dur_s=%.1f exit=%s",
            pid_holder["pid"],
            provider,
            budget_s,
            duration_s,
            proc.returncode,
        )
        return _muse_failure(
            provider=provider,
            model=model,
            code=_failure_code_from_text(message),
            message=f"exit={proc.returncode} {message}",
            retryable=_looks_retryable_provider_error(message) or not message.strip(),
        )
    try:
        text = _parse_muse_jsonl(proc.stdout, expected_model=expected_model)
    except Exception as exc:  # noqa: BLE001 - strict transport parsing
        message = str(exc)
        return _muse_failure(
            provider=provider,
            model=model,
            code="muse_model_mismatch" if "modèle servi" in message else "bad_output",
            message=message,
            retryable=False,
        )
    log.info(
        "[muse_call] pid=%s provider=%s timeout_s=%s dur_s=%.1f outcome=ok model=%s",
        pid_holder["pid"],
        provider,
        budget_s,
        duration_s,
        expected_model,
    )
    return LlmCompletion(provider=provider, model=model, text=text)


@dataclass(frozen=True)
class MuseBackend:
    """Native one-shot Muse transport. Bench-only: no sessions, no tools."""

    provider: str = "muse"
    model: str = DEFAULT_MUSE_MODEL
    muse_bin: str = DEFAULT_MUSE_BIN

    def __post_init__(self) -> None:
        if self.provider != "muse":
            raise ValueError("Muse reporte le provider transport muse")
        parse_muse_model_spec(self.model)

    def execution_capability(self) -> LlmExecutionCapability:
        return LlmExecutionCapability(caged_native_python=False)

    def complete(self, prompt: str, *, timeout_s: int) -> LlmCompletion | LlmFailure:
        muse_model, effort = parse_muse_model_spec(self.model)
        muse_bin = os.getenv("MUSE_BIN") or self.muse_bin
        with tempfile.TemporaryDirectory(prefix="casys-trader-muse-") as workdir:
            prompt_path = os.path.join(workdir, "prompt.txt")
            try:
                with open(prompt_path, "w", encoding="utf-8") as handle:
                    handle.write(prompt)
            except OSError as exc:
                return _muse_failure(
                    provider=self.provider,
                    model=self.model,
                    code="muse_workspace",
                    message=f"prompt Muse inaccessible : {exc}",
                    retryable=False,
                )
            return _run_muse_and_parse(
                build_muse_command(
                    prompt_path,
                    muse_bin=muse_bin,
                    muse_model=muse_model,
                    reasoning_effort=effort,
                ),
                provider=self.provider,
                model=self.model,
                expected_model=muse_model,
                muse_bin=muse_bin,
                timeout_s=timeout_s,
                cwd=workdir,
            )


__all__ = [
    "DEFAULT_MUSE_BIN",
    "DEFAULT_MUSE_EFFORT",
    "DEFAULT_MUSE_MODEL",
    "MUSE_EFFORTS",
    "MuseBackend",
    "build_muse_command",
    "parse_muse_model_spec",
]
