"""scheduler — l'agent pilote ses prochains réveils.

Le timer global est la cadence par défaut. Chaque symbole peut avoir son propre
override piloté par l'agent ; sans override, il suit le timer global. État persisté
pour survivre aux redémarrages.

Le temps est injecté (`now`) pour rester déterministe et testable.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

# Backoff stale — constantes explicites (AX : pas de defaults magiques)
STALE_BACKOFF_BASE_MULTIPLIER: int = 2
STALE_BACKOFF_MAX_MINUTES: float = 120.0
# Streak max persisté : au-delà le wake est déjà cappé (2^2 * 30min = 120min).
# Borne défensive pour éviter OverflowError sur float (2**9999) et garder
# scheduler.json lisible. Valeur 8 = marge confortable au-delà du cap réel.
STALE_BACKOFF_MAX_STREAK: int = 8


class Scheduler:
    def __init__(self, state_path: str | Path):
        self.state_path = Path(state_path)

    def _load_state(self) -> dict:
        if not self.state_path.exists():
            return {"default_next_wake": None, "symbols": {}, "indicator_watches": {}}
        raw = json.loads(self.state_path.read_text())
        if "default_next_wake" not in raw and "next_wake" in raw:
            raw["default_next_wake"] = raw["next_wake"]
        raw.setdefault("default_next_wake", None)
        raw.setdefault("symbols", {})
        raw.setdefault("indicator_watches", {})
        raw.setdefault("stale_streaks", {})
        return raw

    def _save_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2))

    @staticmethod
    def _parse(raw: str | None) -> datetime | None:
        if not raw:
            return None
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def set_next_wake(self, when_iso: str) -> None:
        """Fixe le prochain réveil global par défaut."""
        self.set_default_next_wake(when_iso)

    def set_default_next_wake(self, when_iso: str) -> None:
        """Fixe le prochain réveil global par défaut."""
        datetime.fromisoformat(when_iso)  # fail fast si format invalide
        state = self._load_state()
        state["default_next_wake"] = when_iso
        self._save_state(state)

    def set_next_wake_in(self, *, minutes: float, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        when = (now + timedelta(minutes=minutes)).isoformat()
        self.set_next_wake(when)
        return when

    def set_symbol_next_wake(self, symbol: str, when_iso: str) -> None:
        """Fixe le prochain réveil d'un symbole, qui override le défaut global."""
        datetime.fromisoformat(when_iso)  # fail fast si format invalide
        state = self._load_state()
        state["symbols"][symbol] = when_iso
        self._save_state(state)

    def clear_symbol_next_wake(self, symbol: str) -> None:
        """Retire l'override d'un symbole ; il suit alors le défaut global."""
        state = self._load_state()
        state["symbols"].pop(symbol, None)
        self._save_state(state)

    def symbols_with_wake(self) -> set[str]:
        """Symboles avec un override de réveil — UNE lecture d'état pour tout le cycle
        (has_symbol_wake par symbole = N relectures de scheduler.json)."""
        return {sym for sym, when in self._load_state()["symbols"].items() if when}

    def has_symbol_wake(self, symbol: str) -> bool:
        """True si un override de réveil par symbole est posé (≠ polling par défaut).

        Sert au gate de pertinence (D7) : un tel réveil n'est jamais filtré.
        Note : vrai pour TOUT override par symbole, y compris ceux posés par le
        code (backoff stale, watch) — direction conservatrice voulue (un wake
        posé mérite un passage LLM au réveil, ex. données redevenues fraîches).
        """
        return bool(self._load_state()["symbols"].get(symbol))

    def set_symbol_next_wake_in(
        self,
        symbol: str,
        *,
        minutes: float,
        now: datetime | None = None,
    ) -> str:
        now = now or datetime.now(timezone.utc)
        when = (now + timedelta(minutes=minutes)).isoformat()
        self.set_symbol_next_wake(symbol, when)
        return when

    def next_wake(self, symbol: str | None = None) -> datetime | None:
        state = self._load_state()
        if symbol is not None:
            symbol_wake = state["symbols"].get(symbol)
            if symbol_wake:
                return self._parse(symbol_wake)
        return self._parse(state.get("default_next_wake"))

    def due_symbols(self, symbols: Iterable[str], now: datetime | None = None) -> list[str]:
        """Symboles dus maintenant. Sans timer, un symbole est dû."""
        now = now or datetime.now(timezone.utc)
        due: list[str] = []
        for symbol in symbols:
            nxt = self.next_wake(symbol)
            if nxt is None or nxt <= now:
                due.append(symbol)
        return due

    def seconds_until_wake(
        self,
        symbols: Iterable[str] | None = None,
        now: datetime | None = None,
    ) -> float:
        """Secondes avant le prochain réveil. 0 si dû/non planifié (réveil immédiat)."""
        now = now or datetime.now(timezone.utc)
        if symbols is None:
            nxt = self.next_wake()
            return 0.0 if nxt is None else max(0.0, (nxt - now).total_seconds())

        waits: list[float] = []
        for symbol in symbols:
            nxt = self.next_wake(symbol)
            if nxt is None:
                return 0.0
            waits.append(max(0.0, (nxt - now).total_seconds()))
        return min(waits) if waits else 0.0

    def set_symbol_indicator_watch(self, symbol: str, watch: dict) -> None:
        """Persiste une watch active pour un symbole.

        Plans armés (EXECUTE_ORDER) : des SCÉNARIOS alternatifs — plusieurs
        coexistent sur un symbole, un seul se réalisera (D7, décision Erwan).
        Veilles simples : remplacement par symbole (une question à la fois).
        Les deux familles ne s'évincent pas mutuellement.
        """

        from trader.planning.indicator_watch import is_armed_plan

        state = self._load_state()
        incoming_armed = is_armed_plan(watch)
        watches = {
            watch_id: item
            for watch_id, item in state["indicator_watches"].items()
            if item.get("symbol") != symbol
            or is_armed_plan(item) != incoming_armed  # famille différente : on garde
            or incoming_armed  # plans armés : coexistence
        }
        watches[watch["id"]] = watch
        state["indicator_watches"] = watches
        self._save_state(state)

    def active_indicator_watches(self, now: datetime | None = None) -> list[dict]:
        """Return non-expired watches and purge stale ones from the state file."""
        now = now or datetime.now(timezone.utc)
        state = self._load_state()
        active: list[dict] = []
        kept: dict[str, dict] = {}
        for watch_id, watch in state["indicator_watches"].items():
            expires_at = self._parse(watch.get("expires_at"))
            if expires_at is not None and expires_at <= now:
                continue
            active.append(watch)
            kept[watch_id] = watch
        if kept != state["indicator_watches"]:
            state["indicator_watches"] = kept
            self._save_state(state)
        return active

    def pop_expired_indicator_watches(self, now: datetime | None = None) -> list[dict]:
        """Retire et RETOURNE les veilles expirées (`expires_at <= now`).

        Contrairement à `active_indicator_watches` qui purge en silence, ceci
        rend les expirées à l'appelant pour qu'il émette un event/log par
        expiration (un plan armé ne doit pas s'évaporer sans trace). Idempotent :
        après appel, plus aucune veille expirée à purger.
        """
        now = now or datetime.now(timezone.utc)
        state = self._load_state()
        expired: list[dict] = []
        kept: dict[str, dict] = {}
        for watch_id, watch in state["indicator_watches"].items():
            expires_at = self._parse(watch.get("expires_at"))
            if expires_at is not None and expires_at <= now:
                expired.append(watch)
                continue
            kept[watch_id] = watch
        if expired:
            state["indicator_watches"] = kept
            self._save_state(state)
        return expired

    def remove_indicator_watch(self, watch_id: str) -> None:
        state = self._load_state()
        if watch_id in state["indicator_watches"]:
            state["indicator_watches"].pop(watch_id, None)
            self._save_state(state)

    def reconcile_universe(self, symbols: Iterable[str]) -> None:
        """Purge tout état lié à un symbole absent de l'univers courant.

        Quand l'univers rétrécit (cf. trim 28→15→13), les overrides de réveil,
        streaks stale et veilles des symboles retirés restent gelés dans le
        fichier et polluent le dashboard. Réconcilie `symbols`, `stale_streaks`
        et `indicator_watches` avec l'univers. N'écrit que si quelque chose
        change (idempotent).
        """
        universe = set(symbols)
        state = self._load_state()
        kept_symbols = {s: v for s, v in state["symbols"].items() if s in universe}
        kept_streaks = {
            s: v for s, v in state.get("stale_streaks", {}).items() if s in universe
        }
        kept_watches = {
            watch_id: watch
            for watch_id, watch in state["indicator_watches"].items()
            if watch.get("symbol") in universe
        }
        changed = (
            kept_symbols != state["symbols"]
            or kept_streaks != state.get("stale_streaks", {})
            or kept_watches != state["indicator_watches"]
        )
        if not changed:
            return
        state["symbols"] = kept_symbols
        state["stale_streaks"] = kept_streaks
        state["indicator_watches"] = kept_watches
        self._save_state(state)

    def get_stale_streak(self, symbol: str) -> int:
        """Nombre de réveils stale consécutifs pour ce symbole. 0 si inconnu.

        Borné à STALE_BACKOFF_MAX_STREAK même si le fichier contient une valeur
        supérieure (state corrompu / pré-existant).
        """
        state = self._load_state()
        raw = int(state.get("stale_streaks", {}).get(symbol, 0))
        return min(raw, STALE_BACKOFF_MAX_STREAK)

    def set_stale_streak(self, symbol: str, streak: int) -> None:
        """Enregistre le streak stale d'un symbole, borné à STALE_BACKOFF_MAX_STREAK."""
        state = self._load_state()
        state.setdefault("stale_streaks", {})[symbol] = min(streak, STALE_BACKOFF_MAX_STREAK)
        self._save_state(state)

    def reset_stale_streak(self, symbol: str) -> None:
        """Remet le streak à 0 (appeler dès qu'une donnée fraîche est reçue)."""
        state = self._load_state()
        state.setdefault("stale_streaks", {}).pop(symbol, None)
        self._save_state(state)
