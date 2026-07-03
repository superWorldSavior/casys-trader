"""SqliteScheduler — Scheduler sur substrat SQLite.

Implémente la même API que Scheduler (trader/tools/scheduler.py) avec StateDb
comme backend. 17 méthodes publiques, parité fidèle avec le backend JSON.

Sémantique cruciale :
- `active_indicator_watches(now)` retourne les NON expirées et purge les expirées
  en best-effort (scheduler.py:173-188).
- `pop_expired_indicator_watches(now)` retourne les EXPIRÉES et les DELETE en
  transaction (scheduler.py:190-211). NE PAS confondre les deux.
- Plans armés (`is_armed_plan` = on_trigger=EXECUTE_ORDER + order=dict) : coexistence
  sur un même symbole. Non-armés : remplacement du précédent du même symbole.
- Timestamps canonicalisés UTC (+00:00) pour que WHERE expires_at <= :now lexical
  soit correct (Global Constraints §1d).

Double-write shadow JSON atomique (scheduler.json) après chaque mutation.
Logging : [state_db] (getLogger(__name__), %-style).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from trader.state_db.connection import StateDb
from trader.state_db.shadow import write_json_atomic
from trader.tools.scheduler import STALE_BACKOFF_MAX_STREAK

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers de canonicalisation (UTC +00:00 strict)
# ---------------------------------------------------------------------------


def _canon_ts(raw: str | None) -> str | None:
    """Canonicalise un timestamp ISO → UTC +00:00 pour comparaison SQL lexicale sûre.

    Gère : format naïf (→ UTC), +00:00, Z, et offsets quelconques → +00:00.
    Retourne None si raw est absent/None/vide.
    """
    if not raw:
        return None
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# SqliteScheduler
# ---------------------------------------------------------------------------


class SqliteScheduler:
    """Scheduler sur substrat SQLite (même API publique que Scheduler).

    Args:
        db:        StateDb ouverte avec SCHEDULER_MIGRATION appliquée.
        json_path: chemin du shadow JSON (ex. state/scheduler.json).
                   Si None, pas de double-write shadow.
    """

    def __init__(self, db: StateDb, json_path: Optional[Path] = None) -> None:
        self._db = db
        self._json_path = Path(json_path) if json_path is not None else None

    # ------------------------------------------------------------------
    # Helper interne identique à Scheduler._parse
    # ------------------------------------------------------------------

    @staticmethod
    def _parse(raw: str | None) -> datetime | None:
        """Identique à Scheduler._parse : ISO → datetime UTC."""
        if not raw:
            return None
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    # ------------------------------------------------------------------
    # Réveils globaux
    # ------------------------------------------------------------------

    def set_next_wake(self, when_iso: str) -> None:
        """Fixe le prochain réveil global par défaut."""
        self.set_default_next_wake(when_iso)

    def set_default_next_wake(self, when_iso: str) -> None:
        """Fixe le prochain réveil global par défaut."""
        datetime.fromisoformat(when_iso)  # fail fast si format invalide
        canon = _canon_ts(when_iso)
        with self._db.transaction() as cur:
            cur.execute(
                "INSERT INTO scheduler_meta(key, value) VALUES ('default_next_wake', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (canon,),
            )
        try:
            self._write_shadow()
        except Exception as exc:
            log.warning("[state_db] shadow échec set_default_next_wake: %s", exc)

    def set_next_wake_in(self, *, minutes: float, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        when = (now + timedelta(minutes=minutes)).astimezone(timezone.utc).isoformat()
        self.set_next_wake(when)
        return when

    # ------------------------------------------------------------------
    # Réveils par symbole
    # ------------------------------------------------------------------

    def set_symbol_next_wake(self, symbol: str, when_iso: str) -> None:
        """Fixe le prochain réveil d'un symbole, qui override le défaut global."""
        datetime.fromisoformat(when_iso)  # fail fast si format invalide
        canon = _canon_ts(when_iso)
        with self._db.transaction() as cur:
            cur.execute(
                "INSERT INTO scheduler_symbol_wake(symbol, when_iso) VALUES (?, ?)"
                " ON CONFLICT(symbol) DO UPDATE SET when_iso=excluded.when_iso",
                (symbol, canon),
            )
        try:
            self._write_shadow()
        except Exception as exc:
            log.warning("[state_db] shadow échec set_symbol_next_wake: %s", exc)

    def clear_symbol_next_wake(self, symbol: str) -> None:
        """Retire l'override d'un symbole ; il suit alors le défaut global."""
        with self._db.transaction() as cur:
            cur.execute("DELETE FROM scheduler_symbol_wake WHERE symbol=?", (symbol,))
        try:
            self._write_shadow()
        except Exception as exc:
            log.warning("[state_db] shadow échec clear_symbol_next_wake: %s", exc)

    def set_symbol_next_wake_in(
        self,
        symbol: str,
        *,
        minutes: float,
        now: datetime | None = None,
    ) -> str:
        now = now or datetime.now(timezone.utc)
        when = (now + timedelta(minutes=minutes)).astimezone(timezone.utc).isoformat()
        self.set_symbol_next_wake(symbol, when)
        return when

    def symbols_with_wake(self) -> set[str]:
        """Symboles avec un override de réveil."""
        rows = self._db.query_all("SELECT symbol FROM scheduler_symbol_wake")
        return {r["symbol"] for r in rows}

    def has_symbol_wake(self, symbol: str) -> bool:
        """True si un override de réveil par symbole est posé."""
        row = self._db.query_one(
            "SELECT 1 FROM scheduler_symbol_wake WHERE symbol=?", (symbol,)
        )
        return row is not None

    def next_wake(self, symbol: str | None = None) -> datetime | None:
        if symbol is not None:
            row = self._db.query_one(
                "SELECT when_iso FROM scheduler_symbol_wake WHERE symbol=?", (symbol,)
            )
            if row is not None:
                return self._parse(row["when_iso"])
        row = self._db.query_one(
            "SELECT value FROM scheduler_meta WHERE key='default_next_wake'"
        )
        return self._parse(row["value"] if row else None)

    def due_symbols(
        self, symbols: Iterable[str], now: datetime | None = None
    ) -> list[str]:
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

    # ------------------------------------------------------------------
    # Stale streaks
    # ------------------------------------------------------------------

    def get_stale_streak(self, symbol: str) -> int:
        """Nombre de réveils stale consécutifs. 0 si inconnu, borné à MAX_STREAK."""
        row = self._db.query_one(
            "SELECT streak FROM scheduler_stale_streaks WHERE symbol=?", (symbol,)
        )
        if row is None:
            return 0
        return min(int(row["streak"]), STALE_BACKOFF_MAX_STREAK)

    def set_stale_streak(self, symbol: str, streak: int) -> None:
        """Enregistre le streak stale d'un symbole, borné à STALE_BACKOFF_MAX_STREAK."""
        bounded = min(streak, STALE_BACKOFF_MAX_STREAK)
        with self._db.transaction() as cur:
            cur.execute(
                "INSERT INTO scheduler_stale_streaks(symbol, streak) VALUES (?, ?)"
                " ON CONFLICT(symbol) DO UPDATE SET streak=excluded.streak",
                (symbol, bounded),
            )
        try:
            self._write_shadow()
        except Exception as exc:
            log.warning("[state_db] shadow échec set_stale_streak: %s", exc)

    def reset_stale_streak(self, symbol: str) -> None:
        """Remet le streak à 0 (appeler dès qu'une donnée fraîche est reçue)."""
        with self._db.transaction() as cur:
            cur.execute(
                "DELETE FROM scheduler_stale_streaks WHERE symbol=?", (symbol,)
            )
        try:
            self._write_shadow()
        except Exception as exc:
            log.warning("[state_db] shadow échec reset_stale_streak: %s", exc)

    # ------------------------------------------------------------------
    # Indicator watches
    # ------------------------------------------------------------------

    def set_symbol_indicator_watch(self, symbol: str, watch: dict) -> None:
        """Persiste une watch active pour un symbole.

        Plans armés (EXECUTE_ORDER + order=dict) : coexistence — plusieurs scénarios
        alternatifs coexistent sur un symbole (scheduler.py:149-171).
        Veilles simples (WAKE, WAKE_WITH_ORDER_INTENT) : remplacement du non-armé
        précédent du même symbole. Les deux familles ne s'évincent pas mutuellement.
        """
        from trader.planning.indicator_watch import is_armed_plan  # noqa: PLC0415

        incoming_armed = is_armed_plan(watch)
        watch_id = watch["id"]
        expires_at = _canon_ts(watch.get("expires_at"))
        on_trigger = watch.get("on_trigger", "WAKE")
        watch_json_str = json.dumps(watch)

        with self._db.transaction() as cur:
            if not incoming_armed:
                # Supprime le(s) non-armé(s) précédent(s) du même symbole.
                # Non-armé = NOT (on_trigger='EXECUTE_ORDER' ET order est un objet JSON)
                # — miroir exact de is_armed_plan appliqué aux watches existantes.
                cur.execute(
                    "DELETE FROM scheduler_watches"
                    " WHERE symbol=?"
                    " AND NOT (on_trigger='EXECUTE_ORDER'"
                    "          AND json_type(watch_json, '$.order')='object')",
                    (symbol,),
                )
            # Upsert par id : plans armés coexistent (chacun a son propre id unique),
            # watch non-armée remplace la précédente du même symbole (supprimée ci-dessus).
            cur.execute(
                "INSERT INTO scheduler_watches"
                "(id, seq, symbol, created_at, expires_at, on_trigger, watch_json)"
                " VALUES (?, (SELECT COALESCE(MAX(seq),0)+1 FROM scheduler_watches),"
                " ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                "   seq=(SELECT COALESCE(MAX(seq),0)+1 FROM scheduler_watches),"
                "   symbol=excluded.symbol,"
                "   created_at=excluded.created_at,"
                "   expires_at=excluded.expires_at,"
                "   on_trigger=excluded.on_trigger,"
                "   watch_json=excluded.watch_json",
                (watch_id, symbol, watch.get("created_at"), expires_at, on_trigger, watch_json_str),
            )
        log.debug("[state_db] set_symbol_indicator_watch %s id=%s", symbol, watch_id)
        try:
            self._write_shadow()
        except Exception as exc:
            log.warning("[state_db] shadow échec set_symbol_indicator_watch: %s", exc)

    def active_indicator_watches(self, now: datetime | None = None) -> list[dict]:
        """Retourne les watches NON expirées et purge silencieusement les expirées.

        Sémantique : scheduler.py:173-188.
        - Lit les NON expirées (expires_at IS NULL OR expires_at > now).
        - Purge les expirées en best-effort (DELETE dans une transaction séparée).
        - Régénère le shadow si quelque chose a été purgé.
        - NE PAS confondre avec pop_expired_indicator_watches.
        """
        now_dt = now or datetime.now(timezone.utc)
        now_s = now_dt.astimezone(timezone.utc).isoformat()

        # Lit les watches non-expirées (triées par ordre d'insertion)
        rows = self._db.query_all(
            "SELECT watch_json FROM scheduler_watches"
            " WHERE expires_at IS NULL OR expires_at > ?"
            " ORDER BY seq",
            (now_s,),
        )
        active = [json.loads(r["watch_json"]) for r in rows]

        # Purge les expirées (best-effort — comme le save conditionnel du JSON)
        try:
            with self._db.transaction() as cur:
                cur.execute(
                    "DELETE FROM scheduler_watches"
                    " WHERE expires_at IS NOT NULL AND expires_at <= ?",
                    (now_s,),
                )
                deleted = cur.rowcount
            if deleted:
                self._write_shadow()
        except Exception as exc:
            log.warning("[state_db] active_indicator_watches purge: %s", exc)

        return active

    def pop_expired_indicator_watches(self, now: datetime | None = None) -> list[dict]:
        """Retire et retourne les veilles expirées (`expires_at <= now`).

        Sémantique : scheduler.py:190-211.
        - Retourne les EXPIRÉES à l'appelant pour qu'il les trace (log+event).
        - DELETE en transaction (atomique : lecture + suppression ensemble).
        - Idempotent : après appel, aucune expirée ne reste.
        - NE PAS confondre avec active_indicator_watches.
        """
        now_dt = now or datetime.now(timezone.utc)
        now_s = now_dt.astimezone(timezone.utc).isoformat()
        expired: list[dict] = []

        with self._db.transaction() as cur:
            rows = cur.execute(
                "SELECT watch_json FROM scheduler_watches"
                " WHERE expires_at IS NOT NULL AND expires_at <= ?"
                " ORDER BY seq",
                (now_s,),
            ).fetchall()
            expired = [json.loads(r["watch_json"]) for r in rows]
            if expired:
                cur.execute(
                    "DELETE FROM scheduler_watches"
                    " WHERE expires_at IS NOT NULL AND expires_at <= ?",
                    (now_s,),
                )

        if expired:
            try:
                self._write_shadow()
            except Exception as exc:
                log.warning("[state_db] shadow échec pop_expired: %s", exc)

        return expired

    def remove_indicator_watch(self, watch_id: str) -> None:
        with self._db.transaction() as cur:
            cur.execute("DELETE FROM scheduler_watches WHERE id=?", (watch_id,))
        try:
            self._write_shadow()
        except Exception as exc:
            log.warning("[state_db] shadow échec remove_indicator_watch: %s", exc)

    # ------------------------------------------------------------------
    # Reconciliation univers
    # ------------------------------------------------------------------

    def reconcile_universe(self, symbols: Iterable[str]) -> None:
        """Purge tout état lié à un symbole absent de l'univers courant.

        DELETE des symboles hors-univers sur symbol_wake + stale_streaks + watches,
        en UNE transaction, idempotent (ne régénère le shadow que si changement —
        préserve le comportement « ne sauvegarde que si changement » du JSON).
        """
        universe = list(set(symbols))
        total_deleted = 0

        if not universe:
            # Univers vide → tout supprimer
            with self._db.transaction() as cur:
                cur.execute("DELETE FROM scheduler_symbol_wake")
                total_deleted += cur.rowcount
                cur.execute("DELETE FROM scheduler_stale_streaks")
                total_deleted += cur.rowcount
                cur.execute("DELETE FROM scheduler_watches")
                total_deleted += cur.rowcount
        else:
            placeholders = ",".join("?" * len(universe))
            with self._db.transaction() as cur:
                cur.execute(
                    f"DELETE FROM scheduler_symbol_wake WHERE symbol NOT IN ({placeholders})",  # noqa: S608
                    universe,
                )
                total_deleted += cur.rowcount
                cur.execute(
                    f"DELETE FROM scheduler_stale_streaks WHERE symbol NOT IN ({placeholders})",  # noqa: S608
                    universe,
                )
                total_deleted += cur.rowcount
                cur.execute(
                    f"DELETE FROM scheduler_watches WHERE symbol NOT IN ({placeholders})",  # noqa: S608
                    universe,
                )
                total_deleted += cur.rowcount

        if total_deleted:
            try:
                self._write_shadow()
            except Exception as exc:
                log.warning("[state_db] shadow échec reconcile_universe: %s", exc)

    # ------------------------------------------------------------------
    # Shadow helpers
    # ------------------------------------------------------------------

    def regenerate_shadow(self) -> None:
        """Régénère le shadow scheduler.json depuis les tables SQLite.

        Idempotent et sûr au boot : rattrape un shadow absent ou stale suite à
        un crash entre le COMMIT SQLite et le double-write shadow JSON.
        No-op si json_path est None.
        """
        if self._json_path is None:
            return
        try:
            self._write_shadow()
        except Exception as exc:
            log.warning("[state_db] regenerate_shadow scheduler échec: %s", exc)
            raise

    def _write_shadow(self) -> None:
        """Reconstruit le shadow scheduler.json depuis les tables et l'écrit atomiquement.

        Format identique à scheduler.json (Scheduler) :
          { "default_next_wake": ISO|null,
            "symbols": {sym → iso, ...},
            "stale_streaks": {sym → int, ...},
            "indicator_watches": {id → watch_dict, ...} }

        Watches triées par seq (ordre d'insertion — correctif Codex #4).
        Propage les exceptions — les appelants la wrappent en best-effort.
        No-op si json_path est None.
        """
        if self._json_path is None:
            return

        row = self._db.query_one(
            "SELECT value FROM scheduler_meta WHERE key='default_next_wake'"
        )
        default_next_wake = row["value"] if row else None

        sym_rows = self._db.query_all(
            "SELECT symbol, when_iso FROM scheduler_symbol_wake ORDER BY symbol"
        )
        symbols = {r["symbol"]: r["when_iso"] for r in sym_rows}

        streak_rows = self._db.query_all(
            "SELECT symbol, streak FROM scheduler_stale_streaks ORDER BY symbol"
        )
        stale_streaks = {r["symbol"]: r["streak"] for r in streak_rows}

        watch_rows = self._db.query_all(
            "SELECT id, watch_json FROM scheduler_watches ORDER BY seq"
        )
        indicator_watches = {r["id"]: json.loads(r["watch_json"]) for r in watch_rows}

        write_json_atomic(
            self._json_path,
            {
                "default_next_wake": default_next_wake,
                "symbols": symbols,
                "stale_streaks": stale_streaks,
                "indicator_watches": indicator_watches,
            },
        )
