"""cockpit_events — logique pure pour le panneau de logs live du cockpit.

Parsing, classification et formatage des lignes d'events.jsonl.
Séparé de l'app Textual pour pouvoir être testé sans UI.

Ne lit RIEN dans state/ sauf via read_new_lines (I/O isolée).
N'écrit RIEN.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any


class EventClass(Enum):
    """Classification sémantique d'un événement daemon."""
    CYCLE = auto()              # cycle_started / cycle_completed
    DECISION_EXECUTED = auto()  # décision exécutée (BUY/SELL effectif)
    RISK_REJECT = auto()        # rejet risk:*
    HOLD = auto()               # décision HOLD normale
    STALE = auto()              # stale_market_data / stale_backoff
    WATCH = auto()              # indicator_watch_triggered
    LEARNING = auto()           # learning_consolidated
    ERROR = auto()              # erreur explicite dans l'event
    OTHER = auto()              # tout le reste


@dataclass(frozen=True)
class EventLine:
    """Résultat du formatage d'une ligne d'event."""
    text: str                  # texte human-readable (pas de markup Rich)
    markup_class: EventClass   # pour le coloriage dans le widget


def classify_event(event: dict[str, Any]) -> EventClass:
    """Détermine la classe sémantique d'un event dict.

    Règles, par ordre de priorité :
    1. event == "cycle_started" / "cycle_completed"  → CYCLE
    2. event == "indicator_watch_triggered"          → WATCH
    3. event == "learning_consolidated"              → LEARNING
    4. event == "decision_recorded" :
       - reason.startswith("risk:")                 → RISK_REJECT
       - reason.startswith("stale")                 → STALE
       - executed == True                           → DECISION_EXECUTED
       - sinon                                      → HOLD
    5. "error" dans event_type ou event.get("error")→ ERROR
    6. sinon                                        → OTHER
    """
    event_type = str(event.get("event", ""))
    reason = str(event.get("reason", ""))
    executed = event.get("executed", False)

    if event_type in ("cycle_started", "cycle_completed"):
        return EventClass.CYCLE
    if event_type == "indicator_watch_triggered":
        return EventClass.WATCH
    if event_type == "learning_consolidated":
        return EventClass.LEARNING
    # Events stale au top-level (stale_backoff, stale_market_data comme event direct)
    if event_type.startswith("stale"):
        return EventClass.STALE
    if event_type == "decision_recorded":
        if reason.startswith("risk:"):
            return EventClass.RISK_REJECT
        if reason.startswith("stale"):
            return EventClass.STALE
        if executed:
            return EventClass.DECISION_EXECUTED
        return EventClass.HOLD
    if "error" in event_type or event.get("error"):
        return EventClass.ERROR
    return EventClass.OTHER


def _fmt_ts(ts: str) -> str:
    """Extrait HH:MM:SS depuis un timestamp ISO, retourne '?' en cas d'erreur."""
    if not ts:
        return "?"
    try:
        return ts[11:19]  # HH:MM:SS
    except Exception:
        return ts[:8]


def format_event_line(event: dict[str, Any]) -> EventLine:
    """Transforme un dict d'event en EventLine human-readable.

    Ne lève jamais d'exception.
    """
    try:
        return _format_event_line_inner(event)
    except Exception:
        raw = str(event)
        return EventLine(text=f"[?] {raw[:120]}", markup_class=EventClass.OTHER)


def _format_event_line_inner(event: dict[str, Any]) -> EventLine:
    event_type = str(event.get("event", "?"))
    ts = _fmt_ts(str(event.get("ts", "")))
    cls = classify_event(event)

    if event_type == "cycle_started":
        symbols = event.get("symbols_due", [])
        n = len(symbols) if isinstance(symbols, list) else "?"
        dry = " [dry]" if event.get("dry_run") else ""
        text = f"{ts} ▶ cycle démarré{dry} — {n} symboles"

    elif event_type == "cycle_completed":
        done = event.get("decisions_done", "?")
        calls = event.get("model_calls_used", "?")
        text = f"{ts} ■ cycle terminé — {done} décisions, {calls} appels LLM"

    elif event_type == "decision_recorded":
        symbol = str(event.get("symbol", "?"))
        action = str(event.get("action", "?"))
        reason = str(event.get("reason", ""))
        executed = event.get("executed", False)
        exec_flag = " ✓" if executed else ""
        reason_str = f" [{reason}]" if reason and reason not in ("ok", "hold") else ""
        text = f"{ts} {action} {symbol}{exec_flag}{reason_str}"

    elif event_type == "indicator_watch_triggered":
        symbol = str(event.get("symbol", "?"))
        trigger = str(event.get("on_trigger", ""))
        text = f"{ts} watch — {symbol} ({trigger})"

    elif event_type == "learning_consolidated":
        n = event.get("new_raw_count", "?")
        written = event.get("written", False)
        written_str = " → écrit" if written else ""
        text = f"{ts} learnings consolidés — {n} raws{written_str}"

    else:
        extra_keys = [k for k in event if k not in ("ts", "event")][:3]
        parts = [f"{k}={event[k]!r}" for k in extra_keys]
        text = f"{ts} [{event_type}] {', '.join(parts)}"

    return EventLine(text=text, markup_class=cls)


# Taille maximale lue par tick depuis la position courante (1 MiB).
# Protège le thread UI contre un fichier volumineux au premier lancement.
_READ_CHUNK_SIZE = 1 * 1024 * 1024  # 1 MiB
# Nombre maximal de lignes retournées au premier appel (offset == 0 ou resync).
# Évite d'inonder le buffer RichLog avec tout l'historique.
_TAIL_LINES_INIT = 500


def read_new_lines(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    """Lit les nouvelles lignes JSON complètes depuis `path` à partir de `offset`.

    Contrat :
    - Retourne (lignes_dict_valides, nouvel_offset).
    - Fichier absent → ([], 0).
    - Fichier plus court que offset (troncature/rotation) → relit depuis 0,
      en ne gardant que les _TAIL_LINES_INIT dernières lignes.
    - Premier appel (offset == 0) sur un gros fichier → idem, queue bornée.
    - Ne consomme que les octets jusqu'au dernier '\\n' inclus : un fragment
      sans \\n final n'est pas consommé (l'offset reste au début du fragment).
    - Lignes JSON invalides ignorées silencieusement.
    - Ne lève jamais d'exception.
    """
    try:
        if not path.exists():
            return [], 0

        size = path.stat().st_size
        if size == 0:
            return [], 0

        # Troncature détectée ou premier appel → repositionne au début
        is_init = (offset == 0) or (size < offset)
        read_offset = 0 if is_init else offset

        # --- Lecture bornée : au maximum _READ_CHUNK_SIZE octets ---
        with path.open("rb") as fh:
            fh.seek(read_offset)
            raw = fh.read(_READ_CHUNK_SIZE)

        # --- Ne consommer que jusqu'au dernier '\n' ---
        # Si raw ne contient pas de '\n', il n'y a aucune ligne complète.
        last_newline = raw.rfind(b"\n")
        if last_newline == -1:
            # Fragment partiel sans saut de ligne → rien à consommer
            return [], read_offset

        # On ne traite que la portion jusqu'au dernier '\n' inclus.
        complete_raw = raw[: last_newline + 1]
        new_offset = read_offset + len(complete_raw)

        results: list[dict[str, Any]] = []
        for raw_line in complete_raw.split(b"\n"):
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    results.append(obj)
            except Exception:
                pass  # ligne invalide ignorée

        # --- Borne les résultats au premier appel/resync ---
        if is_init and len(results) > _TAIL_LINES_INIT:
            results = results[-_TAIL_LINES_INIT:]

        return results, new_offset

    except Exception:
        return [], 0
