"""Gate de rotation intégrée au daemon : déclenche run_cli aux clôtures de session."""
from __future__ import annotations

from pathlib import Path

from trader.market.rotation.daemon import maybe_rotate


def _setup(tmp_path: Path, *, last_rotation_at: str) -> tuple[Path, Path]:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "sessions.yaml").write_text('US: "20:00"\n', encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "rotation_state.json").write_text(
        '{"current_hot_set": [], "dwell_days_by_symbol": {}, '
        f'"last_valid_universe": [], "last_rotation_at": "{last_rotation_at}"}}',
        encoding="utf-8",
    )
    return config_dir, state_dir


def test_rotation_due_appelle_run_cli(tmp_path: Path) -> None:
    config_dir, state_dir = _setup(tmp_path, last_rotation_at="2026-06-15T19:00:00+00:00")
    calls: list[str] = []

    def fake_run_cli(cd, sd, *, as_of):
        calls.append(as_of)

    # US ferme 20:00 ; now 21:00 > dernière rotation 19:00 -> due
    tried = maybe_rotate(
        config_dir, state_dir, "2026-06-15T21:00:00+00:00", run_cli=fake_run_cli
    )

    assert tried is True
    assert calls == ["2026-06-15T21:00:00+00:00"]


def test_rotation_non_due_ne_fait_rien(tmp_path: Path) -> None:
    config_dir, state_dir = _setup(tmp_path, last_rotation_at="2026-06-15T20:30:00+00:00")
    calls: list[str] = []

    def fake_run_cli(cd, sd, *, as_of):
        calls.append(as_of)

    # US 20:00 deja avant la derniere rotation (20:30) -> pas due
    tried = maybe_rotate(
        config_dir, state_dir, "2026-06-15T21:00:00+00:00", run_cli=fake_run_cli
    )

    assert tried is False
    assert calls == []


def test_echec_run_cli_ne_propage_pas(tmp_path: Path) -> None:
    config_dir, state_dir = _setup(tmp_path, last_rotation_at="2026-06-15T19:00:00+00:00")

    def boom(cd, sd, *, as_of):
        raise RuntimeError("réseau down")

    # ne doit pas lever : fail-safe, le daemon continue son cycle
    tried = maybe_rotate(config_dir, state_dir, "2026-06-15T21:00:00+00:00", run_cli=boom)
    assert tried is True
