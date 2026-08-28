"""Native, read-only Cursor Agent transport for LLM decisions.

Cursor's ACP adapter only advertises the account default variant of Grok.  It
cannot express the xhigh/non-fast variant required by the decision runtime.
The native CLI can select that exact variant and reports the selected display
name in its ``stream-json`` init event, which this adapter validates on every
turn.
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
from pathlib import Path
from typing import Callable

from trader.domain.llm import LlmCompletion, LlmExecutionCapability, LlmFailure
from trader.infrastructure.llm._errors import _failure_code_from_text, _looks_retryable_provider_error
from trader.infrastructure.llm._subprocess import per_call_timeout_cap_s, terminate_process_group
from trader.support.system.process_env import sanitized_runtime_env

log = logging.getLogger(__name__)

DEFAULT_CURSOR_BIN = "cursor-agent"
# This native model selector is not the public model id stored in decisions.
# It is Cursor's documented selector for Grok 4.6 Extra High, with fast mode
# disabled.  The process init frame below is the runtime proof of that choice.
CURSOR_GROK_XHIGH_NONFAST_MODEL = "cursor-grok-4.6-xhigh"
CURSOR_GROK_XHIGH_NONFAST_DISPLAY_NAME = "Cursor Grok 4.6 Extra High"
DEFAULT_CURSOR_WORKSPACE = str(Path(__file__).resolve().parents[3] / "ops" / "codex-home" / "calc-scratch")
# Cursor Agent enables the account's marketplace plugins even in ``ask`` mode.
# Trader needs no user executable, so a system-only path prevents launchers
# such as ``uvx`` and ``npm`` from leaking personal MCPs into decisions.
CURSOR_RUNTIME_PATH = os.defpath
_ACTIVE_CURSOR_PROCESS_GROUPS: set[int] = set()
_ACTIVE_CURSOR_PROCESS_GROUPS_LOCK = threading.Lock()


def _register_cursor_process_group(pgid: int) -> None:
    with _ACTIVE_CURSOR_PROCESS_GROUPS_LOCK:
        _ACTIVE_CURSOR_PROCESS_GROUPS.add(pgid)


def _unregister_cursor_process_group(pgid: int) -> None:
    with _ACTIVE_CURSOR_PROCESS_GROUPS_LOCK:
        _ACTIVE_CURSOR_PROCESS_GROUPS.discard(pgid)


def _terminate_active_cursor_process_groups() -> None:
    """Last-resort cleanup when daemon worker threads outlive main shutdown."""

    with _ACTIVE_CURSOR_PROCESS_GROUPS_LOCK:
        groups = tuple(_ACTIVE_CURSOR_PROCESS_GROUPS)
        _ACTIVE_CURSOR_PROCESS_GROUPS.clear()
    for pgid in groups:
        terminate_process_group(pgid)


atexit.register(_terminate_active_cursor_process_groups)


def build_cursor_agent_command(
    prompt: str,
    *,
    cursor_bin: str = DEFAULT_CURSOR_BIN,
    cursor_model: str = CURSOR_GROK_XHIGH_NONFAST_MODEL,
    workspace: str = DEFAULT_CURSOR_WORKSPACE,
    chat_id: str | None = None,
) -> list[str]:
    """Build the only allowed Cursor invocation for trading decisions.

    ``ask`` is Cursor's documented read-only Q&A mode.  It is always paired
    with the native sandbox.  It deliberately never accepts force, MCP
    approval, shell, or write flags, even when ``CASYS_AGENT_EXEC=1`` enables
    native calculation tools for another backend. ``--trust`` only suppresses
    workspace onboarding; the workspace itself is an isolated app scratch.
    """

    command = [
        cursor_bin,
        "--print",
        "--output-format",
        "stream-json",
        "--stream-partial-output",
        "--mode",
        "ask",
        "--sandbox",
        "enabled",
        "--trust",
        "--model",
        cursor_model,
        "--workspace",
        workspace,
    ]
    if chat_id is not None:
        command += ["--resume", chat_id]
    return [*command, prompt]


def _cursor_child_env(runtime_config_dir: str) -> dict[str, str]:
    """Build the least-privilege environment for one Cursor decision session.

    HOME stays the operator's: Cursor's login is bound to that account
    (macOS Keychain and ``~/.cursor`` paths that ignore ``CURSOR_*_DIR``).
    A temporary HOME unauthenticates the CLI. Isolate config/data only.
    """

    child_env = dict(os.environ)
    child_env.pop("CURSOR_CONFIG_DIR", None)
    child_env.pop("CURSOR_DATA_DIR", None)
    child_env["CURSOR_CONFIG_DIR"] = runtime_config_dir
    child_env["CURSOR_DATA_DIR"] = runtime_config_dir
    child_env["PATH"] = CURSOR_RUNTIME_PATH
    return sanitized_runtime_env(child_env)


def _run_cursor_command(
    command: list[str],
    *,
    timeout_s: int,
    cursor_runtime_config_dir: str,
    on_pid: Callable[[int], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=os.name == "posix",
            env=_cursor_child_env(cursor_runtime_config_dir),
        )
        _register_cursor_process_group(proc.pid)
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
                _unregister_cursor_process_group(proc.pid)

    return subprocess.CompletedProcess(
        args=command,
        returncode=proc.returncode if proc is not None else 1,
        stdout=stdout,
        stderr=stderr,
    )


def _parse_cursor_stream(
    output: str,
    *,
    expected_chat_id: str | None = None,
) -> tuple[str, str]:
    """Extract final text and verify Cursor's runtime-selected model.

    The CLI init event is not advisory metadata: a mismatch is a fail-closed
    transport error, so a high/fast account default can never become a trade
    decision under this backend.
    """

    init_chat_id: str | None = None
    result_chat_id: str | None = None
    model: str | None = None
    result_text: str | None = None
    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("sortie stream-json Cursor invalide") from exc
        if not isinstance(event, dict):
            raise RuntimeError("événement stream-json Cursor invalide")
        event_type = event.get("type")
        if event_type == "system" and event.get("subtype") == "init":
            init_chat_id = str(event.get("session_id") or "").strip() or None
            model = str(event.get("model") or "").strip() or None
        elif event_type == "result":
            result_chat_id = str(event.get("session_id") or "").strip() or None
            if event.get("subtype") == "success" and event.get("is_error") is False:
                candidate = event.get("result")
                result_text = candidate if isinstance(candidate, str) else None

    if model != CURSOR_GROK_XHIGH_NONFAST_DISPLAY_NAME:
        raise RuntimeError(
            "appel Cursor refusé : modèle annoncé "
            f"{model or 'absent'!r}, attendu {CURSOR_GROK_XHIGH_NONFAST_DISPLAY_NAME!r}"
        )
    if init_chat_id is None or result_chat_id != init_chat_id:
        raise RuntimeError("appel Cursor refusé : session stream-json absente ou incohérente")
    if expected_chat_id is not None and init_chat_id != expected_chat_id:
        raise RuntimeError("appel Cursor refusé : reprise de session inattendue")
    if result_text is None:
        raise RuntimeError("appel Cursor refusé : résultat stream-json non réussi")
    return result_text, init_chat_id


def _cursor_failure(*, provider: str, model: str, code: str, message: str, retryable: bool) -> LlmFailure:
    return LlmFailure(provider=provider, model=model, code=code, message=message, retryable=retryable)


def _run_cursor_and_parse(
    command: list[str],
    *,
    provider: str,
    model: str,
    cursor_bin: str,
    timeout_s: int,
    cursor_runtime_config_dir: str,
    expected_chat_id: str | None = None,
) -> tuple[LlmCompletion | LlmFailure, str | None]:
    budget_s = min(int(timeout_s), per_call_timeout_cap_s())
    resolved_cursor_bin = shutil.which(cursor_bin)
    if resolved_cursor_bin is None:
        return (
            _cursor_failure(
                provider=provider,
                model=model,
                code="cursor_unavailable",
                message=f"binaire '{cursor_bin}' introuvable",
                retryable=True,
            ),
            None,
        )
    command = [resolved_cursor_bin, *command[1:]]
    pid_holder: dict[str, int | None] = {"pid": None}
    started = time.monotonic()
    try:
        proc = _run_cursor_command(
            command,
            timeout_s=budget_s + 15,
            cursor_runtime_config_dir=cursor_runtime_config_dir,
            on_pid=lambda pid: pid_holder.__setitem__("pid", pid),
        )
    except subprocess.TimeoutExpired:
        return (
            _cursor_failure(
                provider=provider,
                model=model,
                code="timeout",
                message=f"> {budget_s}s",
                retryable=True,
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001 - provider boundary
        message = str(exc)
        return (
            _cursor_failure(
                provider=provider,
                model=model,
                code=_failure_code_from_text(message),
                message=message,
                retryable=_looks_retryable_provider_error(message),
            ),
            None,
        )

    duration_s = time.monotonic() - started
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "")[:500]
        log.warning(
            "[cursor_call] pid=%s provider=%s timeout_s=%s dur_s=%.1f exit=%s",
            pid_holder["pid"],
            provider,
            budget_s,
            duration_s,
            proc.returncode,
        )
        return (
            _cursor_failure(
                provider=provider,
                model=model,
                code=_failure_code_from_text(message),
                message=f"exit={proc.returncode} {message}",
                retryable=_looks_retryable_provider_error(message) or not message.strip(),
            ),
            None,
        )
    try:
        text, chat_id = _parse_cursor_stream(proc.stdout, expected_chat_id=expected_chat_id)
    except Exception as exc:  # noqa: BLE001 - strict transport parsing
        message = str(exc)
        return (
            _cursor_failure(
                provider=provider,
                model=model,
                code="cursor_model_mismatch" if "modèle annoncé" in message else "bad_output",
                message=message,
                retryable=False,
            ),
            None,
        )
    log.info(
        "[cursor_call] pid=%s provider=%s timeout_s=%s dur_s=%.1f outcome=ok model=%s",
        pid_holder["pid"],
        provider,
        budget_s,
        duration_s,
        CURSOR_GROK_XHIGH_NONFAST_DISPLAY_NAME,
    )
    return LlmCompletion(provider=provider, model=model, text=text), chat_id


@dataclass
class CursorAgentSession:
    provider: str
    model: str
    workspace: str
    runtime_config: tempfile.TemporaryDirectory
    runtime_workspace: tempfile.TemporaryDirectory | None = None
    chat_id: str | None = None
    cursor_bin: str = DEFAULT_CURSOR_BIN
    cursor_model: str = CURSOR_GROK_XHIGH_NONFAST_MODEL
    # Kept as causal runtime metadata; it is not caller-configurable in
    # practice because every command builder forces ask+sandbox.
    allow_native_exec: bool = False

    def execution_capability(self) -> LlmExecutionCapability:
        return LlmExecutionCapability(caged_native_python=False)

    def send(self, prompt: str, *, timeout_s: int, call_ctx: dict | None = None) -> LlmCompletion | LlmFailure:
        del call_ctx
        result, received_chat_id = _run_cursor_and_parse(
            build_cursor_agent_command(
                prompt,
                cursor_bin=self.cursor_bin,
                cursor_model=self.cursor_model,
                workspace=self.workspace,
                chat_id=self.chat_id,
            ),
            provider=self.provider,
            model=self.model,
            cursor_bin=self.cursor_bin,
            timeout_s=timeout_s,
            cursor_runtime_config_dir=self.runtime_config.name,
            expected_chat_id=self.chat_id,
        )
        if isinstance(result, LlmCompletion) and self.chat_id is None:
            self.chat_id = received_chat_id
        return result

    def close(self) -> None:
        # The native CLI exposes no chat-close operation.  The disposable
        # config/data dir prevents local session/auth state from surviving.
        try:
            self.runtime_config.cleanup()
        finally:
            if self.runtime_workspace is not None:
                self.runtime_workspace.cleanup()


@dataclass(frozen=True)
class CursorAgentBackend:
    """Cursor transport with a fixed Grok 4.6 xhigh/non-fast runtime contract."""

    provider: str = "cursor-agent"
    model: str = CURSOR_GROK_XHIGH_NONFAST_MODEL
    cursor_bin: str = DEFAULT_CURSOR_BIN
    cursor_model: str = CURSOR_GROK_XHIGH_NONFAST_MODEL
    session_label: str | None = None
    # Production sessions use a disposable workspace outside the repository.
    # An explicit absolute path remains available to deterministic harnesses.
    workspace: str | None = None
    allow_native_exec: bool = False
    agent: str = "cursor"

    def __post_init__(self) -> None:
        if self.provider != "cursor-agent":
            raise ValueError("Cursor Agent reporte le provider transport cursor-agent")
        if self.allow_native_exec is not False:
            raise ValueError("Cursor Agent est strictement read-only dans le runtime Trader")
        if self.model != CURSOR_GROK_XHIGH_NONFAST_MODEL or self.cursor_model != CURSOR_GROK_XHIGH_NONFAST_MODEL:
            raise ValueError("Cursor Agent Trader impose Grok 4.6 xhigh non-fast")
        if self.workspace is not None and not Path(self.workspace).is_absolute():
            raise ValueError("workspace Cursor Agent doit être absolu")

    def execution_capability(self) -> LlmExecutionCapability:
        return LlmExecutionCapability(caged_native_python=False)

    def open_session(self, name: str, *, timeout_s: int) -> CursorAgentSession | LlmFailure:
        del name, timeout_s
        runtime_workspace: tempfile.TemporaryDirectory | None = None
        try:
            workspace = self.workspace
            if workspace is None:
                runtime_workspace = tempfile.TemporaryDirectory(prefix="casys-trader-cursor-workspace-")
                workspace = runtime_workspace.name
            else:
                Path(workspace).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            if runtime_workspace is not None:
                runtime_workspace.cleanup()
            return _cursor_failure(
                provider=self.provider,
                model=self.model,
                code="cursor_workspace",
                message=f"workspace Cursor inaccessible : {exc}",
                retryable=False,
            )
        return CursorAgentSession(
            provider=self.provider,
            model=self.model,
            workspace=workspace,
            runtime_config=tempfile.TemporaryDirectory(prefix="casys-trader-cursor-"),
            runtime_workspace=runtime_workspace,
            cursor_bin=self.cursor_bin,
            cursor_model=self.cursor_model,
        )

    def complete(self, prompt: str, *, timeout_s: int) -> LlmCompletion | LlmFailure:
        session = self.open_session("oneshot", timeout_s=timeout_s)
        if isinstance(session, LlmFailure):
            return session
        try:
            return session.send(prompt, timeout_s=timeout_s)
        finally:
            session.close()


__all__ = [
    "CURSOR_GROK_XHIGH_NONFAST_DISPLAY_NAME",
    "CURSOR_GROK_XHIGH_NONFAST_MODEL",
    "CursorAgentBackend",
    "CursorAgentSession",
    "DEFAULT_CURSOR_BIN",
    "DEFAULT_CURSOR_WORKSPACE",
    "build_cursor_agent_command",
]
