"""Durable learned-state snapshots for the non-authoritative world model.

A cold daemon replays the whole outcome ledger to rebuild predictor weights
(``WorldModelService._hydrate_baseline``).  That replay is pure overhead when
the ledger did not change: the snapshot stores the learned state on disk so a
restart restores weights instead of re-training them.

Soundness basis (audited, locked by tests):

* Predictor learned state mutates only inside ``apply_outcome`` paths (plus
  ``reset_for_replay``).  ``predict`` only lazily creates content-free empty
  horizon states, so restoring weights while re-observing episodes is
  exactly equivalent to a full replay.
* The service already fingerprints the store content on every hydrate pass
  (eligible episodes + active outcome leaves).  A snapshot is usable only
  when those fingerprints still match, so new labels or corrections always
  force a real replay.
* Episodes stay canonical in the ledger and are never snapshotted.  The file
  holds learned state only, plus the gate fields that prove it is current.

Failure policy: every snapshot problem fails open to a full replay, loudly.
The restore/save outcome (including the skip reason) is recorded in the
mature report, and abnormal causes (corruption, failed restore, failed save)
also warn.  Deleting the file is the supported hatch to force one replay.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

SNAPSHOT_FORMAT_VERSION = 1

_REQUIRED_KEYS = frozenset(
    {
        "snapshot_format",
        "saved_at",
        "horizons",
        "eligible_episode_fingerprint",
        "active_outcome_fingerprint",
        "model_hydrated_through",
        "predictors",
    }
)

class SnapshotError(ValueError):
    """A snapshot cannot be read, verified, gated, or restored.

    The message is a stable machine-readable reason (``snapshot_*``) used
    in mature reports and logs.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def to_jsonable(value: object) -> object:
    """Normalize dataclass-shaped values to JSON-native structures.

    Predictor contracts must survive a JSON round trip unchanged so the
    gate can compare file content with live values by equality.
    """

    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (frozenset, set)):
        return sorted((to_jsonable(item) for item in value), key=repr)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _canonical(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _state_sha256(state: object) -> str:
    return sha256(_canonical(state)).hexdigest()


def _check_envelope(payload: object) -> list[dict[str, Any]]:
    """Validate envelope shape and gate-field types.  Shared by read and write.

    Write-side validation fails fast at save time.  Predictor-state
    agreement (writer emits what the reader accepts) is enforced strictly
    at restore and locked by round-trip tests, not re-validated here: a
    writer bug surfaces as a loud restore skip plus a healing replay.
    """

    if not isinstance(payload, dict) or _REQUIRED_KEYS - payload.keys():
        raise SnapshotError("snapshot_envelope_invalid")
    if type(payload["snapshot_format"]) is not int or payload["snapshot_format"] != SNAPSHOT_FORMAT_VERSION:
        raise SnapshotError("snapshot_format_unsupported")
    if not isinstance(payload["saved_at"], str):
        raise SnapshotError("snapshot_envelope_invalid:saved_at")
    if not isinstance(payload["horizons"], list) or not all(isinstance(item, str) for item in payload["horizons"]):
        raise SnapshotError("snapshot_envelope_invalid:horizons")
    for key in ("eligible_episode_fingerprint", "active_outcome_fingerprint", "model_hydrated_through"):
        if payload[key] is not None and not isinstance(payload[key], str):
            raise SnapshotError(f"snapshot_envelope_invalid:{key}")
    through = payload["model_hydrated_through"]
    if through is not None:
        try:
            parsed = datetime.fromisoformat(through)
        except ValueError as exc:
            raise SnapshotError("snapshot_envelope_invalid:model_hydrated_through") from exc
        if parsed.tzinfo is None:
            raise SnapshotError("snapshot_envelope_invalid:model_hydrated_through")
    entries = payload["predictors"]
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise SnapshotError("snapshot_envelope_invalid:predictors")
    for entry in entries:
        if not isinstance(entry.get("model_id"), str) or not isinstance(entry.get("model_version"), str):
            raise SnapshotError("snapshot_envelope_invalid:predictor_identity")
        if not isinstance(entry.get("contract"), dict) or not isinstance(entry.get("state"), dict):
            raise SnapshotError("snapshot_envelope_invalid:predictor_state")
    return entries


def write_snapshot(path: str | Path, payload: dict[str, object]) -> None:
    """Checksum and atomically persist a snapshot envelope.

    State checksums are injected into shallow copies: the caller's payload
    is never mutated.  Raises :class:`SnapshotError` without touching a
    previously saved file when the write cannot complete.
    """

    entries = _check_envelope(payload)
    try:
        stamped = [{**entry, "state_sha256": _state_sha256(entry["state"])} for entry in entries]
        text = json.dumps({**payload, "predictors": stamped}, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    except (TypeError, ValueError) as exc:
        raise SnapshotError(f"snapshot_unserializable:{type(exc).__name__}:{exc}") from exc
    target = Path(path)
    tmp_path = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(tmp_path, target)
    except OSError as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise SnapshotError(f"snapshot_unwritable:{type(exc).__name__}:{exc}") from exc


def read_snapshot(path: str | Path) -> dict[str, Any]:
    """Read and verify a snapshot envelope.  Raises :class:`SnapshotError`."""

    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SnapshotError("snapshot_missing") from exc
    except OSError as exc:
        raise SnapshotError(f"snapshot_unreadable:{type(exc).__name__}:{exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"snapshot_corrupt:{exc}") from exc
    entries = _check_envelope(payload)
    for entry in entries:
        if not isinstance(entry.get("state_sha256"), str):
            raise SnapshotError("snapshot_envelope_invalid:predictor_entry")
        if entry["state_sha256"] != _state_sha256(entry["state"]):
            raise SnapshotError("snapshot_state_checksum_mismatch")
    return payload


def snapshot_gate_mismatch(
    payload: dict[str, Any],
    *,
    eligible_episode_fingerprint: str | None,
    active_outcome_fingerprint: str | None,
    predictor_contracts: dict[tuple[str, str], dict[str, object]],
    horizons: list[str] | tuple[str, ...],
    now: datetime | None,
) -> str | None:
    """Return why a verified snapshot must not be restored, or ``None``.

    ``payload`` must come from :func:`read_snapshot` (checksums verified).
    ``predictor_contracts`` maps live ``(model_id, model_version)`` pairs to
    their current JSON-stable contracts.
    """

    if payload["eligible_episode_fingerprint"] != eligible_episode_fingerprint:
        return "snapshot_eligible_episodes_changed"
    if payload["active_outcome_fingerprint"] != active_outcome_fingerprint:
        return "snapshot_active_outcomes_changed"
    entries = payload["predictors"]
    assert isinstance(entries, list)
    stored_identities = {(str(entry["model_id"]), str(entry["model_version"])) for entry in entries}
    if stored_identities != set(predictor_contracts):
        return "snapshot_predictor_set_changed"
    for entry in entries:
        identity = (str(entry["model_id"]), str(entry["model_version"]))
        if entry["contract"] != predictor_contracts[identity]:
            return f"snapshot_predictor_contract_changed:{identity[0]}:{identity[1]}"
    if sorted(payload["horizons"]) != sorted(horizons):
        return "snapshot_horizons_changed"
    through = payload["model_hydrated_through"]
    if now is None or through is None:
        # Restore needs a causal bound on both sides: unbounded weights
        # would diverge from a bounded replay (and vice versa).
        return "snapshot_cutoff_unknown"
    if now < datetime.fromisoformat(str(through)):
        return "snapshot_cutoff_moved_backwards"
    return None
