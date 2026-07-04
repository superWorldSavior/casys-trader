"""Tests de persistance des items de news (spec §3.1).

Couverture :
- append + relecture du fichier du jour
- dédup uuid intra-jour : même instance
- dédup uuid intra-jour : après restart (nouvelle instance)
- items sans uuid → toujours appendés (pas de dédup)
- erreur d'écriture avalée (monkeypatch → append_items ne lève pas)
- news_snapshot : erreur d'archive n'affecte pas le résultat du fetch
- news_snapshot : les items sont archivés via l'archive injectée
- purge : fichier de 61j supprimé au premier append du jour
- purge : fichier de 59j conservé
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trader.market import news_feed as nf

UTC = timezone.utc
NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Isole le cache module-level entre tests."""
    nf.reset_cache()
    yield
    nf.reset_cache()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_item(uuid: str | None, title: str = "Titre", ts: datetime | None = None) -> dict:
    """Construit un item minimal compatible yfinance."""
    ts = ts or NOW
    item: dict = {"title": title, "publisher": "Reuters", "link": f"https://example.com/{uuid}"}
    if uuid is not None:
        item["uuid"] = uuid
    item["providerPublishTime"] = int(ts.timestamp())
    return item


def _read_rows(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# Append + relecture
# ---------------------------------------------------------------------------


def test_append_then_read_back(tmp_path):
    """Les items appendés sont lisibles dans le fichier du jour."""
    arc = nf.NewsItemsArchive(tmp_path / "news_items")
    items = [
        _make_item("uuid-1", "Titre 1"),
        _make_item("uuid-2", "Titre 2"),
    ]
    arc.append_items("AAPL", items, now=NOW)

    day_file = tmp_path / "news_items" / f"{NOW.strftime('%Y-%m-%d')}.jsonl"
    assert day_file.exists()
    rows = _read_rows(day_file)
    assert len(rows) == 2
    assert {r["uuid"] for r in rows} == {"uuid-1", "uuid-2"}
    assert all(r["symbol"] == "AAPL" for r in rows)
    assert all(r["fetched_at"] == NOW.isoformat() for r in rows)
    assert all(r["publisher"] == "Reuters" for r in rows)


def test_append_sets_published_at(tmp_path):
    """published_at est correctement extrait et iso-formaté dans la ligne."""
    arc = nf.NewsItemsArchive(tmp_path / "news_items")
    pub = datetime(2026, 6, 22, 10, 0, tzinfo=UTC)
    item = {"uuid": "x", "title": "T", "providerPublishTime": int(pub.timestamp())}
    arc.append_items("MSFT", [item], now=NOW)

    day_file = tmp_path / "news_items" / f"{NOW.strftime('%Y-%m-%d')}.jsonl"
    row = _read_rows(day_file)[0]
    assert row["published_at"] is not None
    dt = datetime.fromisoformat(row["published_at"])
    assert abs((dt.astimezone(UTC) - pub).total_seconds()) < 1


# ---------------------------------------------------------------------------
# Dédup uuid — même instance
# ---------------------------------------------------------------------------


def test_dedup_same_instance(tmp_path):
    """Un uuid déjà vu dans la même instance n'est pas réappendé."""
    arc = nf.NewsItemsArchive(tmp_path / "news_items")
    item = _make_item("uuid-A")
    # Premier append
    arc.append_items("AAPL", [item], now=NOW)
    # Deuxième append avec le même uuid
    arc.append_items("AAPL", [item], now=NOW)

    day_file = tmp_path / "news_items" / f"{NOW.strftime('%Y-%m-%d')}.jsonl"
    rows = _read_rows(day_file)
    assert len(rows) == 1, f"attendu 1 ligne, obtenu {len(rows)}"


def test_dedup_different_uuid_both_written(tmp_path):
    """Deux items avec des uuids distincts sont tous les deux écrits."""
    arc = nf.NewsItemsArchive(tmp_path / "news_items")
    arc.append_items("AAPL", [_make_item("uuid-1")], now=NOW)
    arc.append_items("AAPL", [_make_item("uuid-2")], now=NOW)

    day_file = tmp_path / "news_items" / f"{NOW.strftime('%Y-%m-%d')}.jsonl"
    rows = _read_rows(day_file)
    assert len(rows) == 2
    assert {r["uuid"] for r in rows} == {"uuid-1", "uuid-2"}


# ---------------------------------------------------------------------------
# Dédup uuid — après restart (nouvelle instance)
# ---------------------------------------------------------------------------


def test_dedup_after_restart(tmp_path):
    """Une nouvelle instance relit le fichier du jour et dédupe les uuids existants."""
    base = tmp_path / "news_items"
    item = _make_item("uuid-R")

    # Instance 1 : écrit l'item
    arc1 = nf.NewsItemsArchive(base)
    arc1.append_items("AAPL", [item], now=NOW)

    # Instance 2 (simule un restart) : tente de réécrire le même item
    arc2 = nf.NewsItemsArchive(base)
    arc2.append_items("AAPL", [item], now=NOW)

    day_file = base / f"{NOW.strftime('%Y-%m-%d')}.jsonl"
    rows = _read_rows(day_file)
    assert len(rows) == 1, f"après restart, uuid dupliqué : {len(rows)} lignes"


def test_new_uuid_written_after_restart(tmp_path):
    """Après restart, un uuid non encore vu est bien écrit."""
    base = tmp_path / "news_items"
    arc1 = nf.NewsItemsArchive(base)
    arc1.append_items("AAPL", [_make_item("uuid-X")], now=NOW)

    arc2 = nf.NewsItemsArchive(base)
    arc2.append_items("AAPL", [_make_item("uuid-Y")], now=NOW)

    day_file = base / f"{NOW.strftime('%Y-%m-%d')}.jsonl"
    rows = _read_rows(day_file)
    assert len(rows) == 2
    assert {r["uuid"] for r in rows} == {"uuid-X", "uuid-Y"}


# ---------------------------------------------------------------------------
# Items sans uuid → pas de dédup
# ---------------------------------------------------------------------------


def test_items_without_uuid_always_appended(tmp_path):
    """Les items sans uuid ne peuvent pas être dédupliqués : tous sont écrits."""
    arc = nf.NewsItemsArchive(tmp_path / "news_items")
    item_no_uuid = {"title": "No uuid", "publisher": "AFP", "providerPublishTime": int(NOW.timestamp())}
    arc.append_items("AAPL", [item_no_uuid], now=NOW)
    arc.append_items("AAPL", [item_no_uuid], now=NOW)

    day_file = tmp_path / "news_items" / f"{NOW.strftime('%Y-%m-%d')}.jsonl"
    rows = _read_rows(day_file)
    assert len(rows) == 2
    assert all(r["uuid"] is None for r in rows)


# ---------------------------------------------------------------------------
# Erreur d'écriture avalée
# ---------------------------------------------------------------------------


def test_write_error_swallowed(tmp_path, monkeypatch):
    """Une OSError lors de l'ouverture du fichier est avalée (best-effort)."""
    arc = nf.NewsItemsArchive(tmp_path / "news_items")
    (tmp_path / "news_items").mkdir()

    # Forcer une erreur lors du open()
    real_open = Path.open

    def _broken_open(self, mode="r", **kwargs):
        if "a" in str(mode):
            raise OSError("disk full")
        return real_open(self, mode, **kwargs)

    monkeypatch.setattr(Path, "open", _broken_open)

    # Ne doit absolument pas lever
    arc.append_items("AAPL", [_make_item("uuid-Z")], now=NOW)


def test_news_snapshot_archive_error_does_not_affect_result(tmp_path, monkeypatch):
    """Une erreur de l'archive n'affecte pas le résultat du fetch."""
    arc = nf.NewsItemsArchive(tmp_path / "news_items")

    # Monkeypatch append_items pour simuler une erreur non gérée
    def _raises(*args, **kwargs):
        raise RuntimeError("archive indisponible")

    monkeypatch.setattr(arc, "append_items", _raises)

    items = [_make_item("uuid-err")]
    raw = nf.RawNews(mapped=True, earnings_dates=(), news_count=1, items=tuple(items))

    # Le snapshot doit être retourné correctement malgré l'erreur d'archive
    snap = nf.news_snapshot("AAPL", now=NOW, fetcher=lambda s, *, now: raw, archive=arc)

    assert snap["news_coverage"] == "ok"
    assert snap["news_count"] == 1
    assert snap["source"] == "yahoo"


# ---------------------------------------------------------------------------
# Intégration news_snapshot → archive
# ---------------------------------------------------------------------------


def test_news_snapshot_archives_items_on_success(tmp_path):
    """Les items retournés par le fetcher sont archivés dans l'archive injectée."""
    arc = nf.NewsItemsArchive(tmp_path / "news_items")
    items = [_make_item("uuid-s1", "T1"), _make_item("uuid-s2", "T2")]
    raw = nf.RawNews(mapped=True, earnings_dates=(), news_count=2, items=tuple(items))

    snap = nf.news_snapshot("AAPL", now=NOW, fetcher=lambda s, *, now: raw, archive=arc)

    assert snap["news_count"] == 2
    day_file = tmp_path / "news_items" / f"{NOW.strftime('%Y-%m-%d')}.jsonl"
    assert day_file.exists()
    rows = _read_rows(day_file)
    assert len(rows) == 2
    assert {r["uuid"] for r in rows} == {"uuid-s1", "uuid-s2"}


def test_news_snapshot_no_archive_if_items_empty(tmp_path):
    """Si RawNews.items est vide, aucun fichier n'est créé."""
    arc = nf.NewsItemsArchive(tmp_path / "news_items")
    raw = nf.RawNews(mapped=True, earnings_dates=(), news_count=0, items=())

    nf.news_snapshot("AAPL", now=NOW, fetcher=lambda s, *, now: raw, archive=arc)

    day_file = tmp_path / "news_items" / f"{NOW.strftime('%Y-%m-%d')}.jsonl"
    assert not day_file.exists()


def test_news_snapshot_no_archive_called_on_fetcher_error(tmp_path):
    """En cas d'erreur du fetcher, l'archive n'est pas appelée."""
    arc = nf.NewsItemsArchive(tmp_path / "news_items")

    snap = nf.news_snapshot(
        "AAPL",
        now=NOW,
        fetcher=lambda s, *, now: (_ for _ in ()).throw(RuntimeError("down")),
        archive=arc,
    )

    assert snap["news_coverage"] == "error"
    day_file = tmp_path / "news_items" / f"{NOW.strftime('%Y-%m-%d')}.jsonl"
    assert not day_file.exists()


# ---------------------------------------------------------------------------
# Purge : 61j supprime, 59j garde
# ---------------------------------------------------------------------------


def test_purge_61_days_deleted(tmp_path):
    """Un fichier de 61 jours est supprimé lors du premier append du jour."""
    base = tmp_path / "news_items"
    base.mkdir()

    old_date = (NOW - timedelta(days=61)).strftime("%Y-%m-%d")
    old_file = base / f"{old_date}.jsonl"
    old_file.write_text("")

    arc = nf.NewsItemsArchive(base)
    arc.append_items("AAPL", [_make_item("uuid-p")], now=NOW)

    assert not old_file.exists(), f"Le fichier {old_date} (61j) aurait dû être supprimé"


def test_purge_59_days_kept(tmp_path):
    """Un fichier de 59 jours est conservé lors du premier append du jour."""
    base = tmp_path / "news_items"
    base.mkdir()

    recent_date = (NOW - timedelta(days=59)).strftime("%Y-%m-%d")
    recent_file = base / f"{recent_date}.jsonl"
    recent_file.write_text("")

    arc = nf.NewsItemsArchive(base)
    arc.append_items("AAPL", [_make_item("uuid-q")], now=NOW)

    assert recent_file.exists(), f"Le fichier {recent_date} (59j) aurait dû être conservé"


def test_purge_exactly_60_days_kept(tmp_path):
    """Un fichier exactement à 60j n'est pas purgé (seuil strict > 60)."""
    base = tmp_path / "news_items"
    base.mkdir()

    boundary_date = (NOW - timedelta(days=60)).strftime("%Y-%m-%d")
    boundary_file = base / f"{boundary_date}.jsonl"
    boundary_file.write_text("")

    arc = nf.NewsItemsArchive(base)
    arc.append_items("AAPL", [_make_item("uuid-b")], now=NOW)

    assert boundary_file.exists(), "Le fichier à exactement 60j doit être conservé"


def test_purge_triggered_once_per_day(tmp_path):
    """La purge n'est déclenchée qu'une seule fois par jour (optimisation)."""
    base = tmp_path / "news_items"
    base.mkdir()

    old_date = (NOW - timedelta(days=61)).strftime("%Y-%m-%d")
    old_file = base / f"{old_date}.jsonl"

    arc = nf.NewsItemsArchive(base)
    # Premier append : purge déclenchée
    arc.append_items("AAPL", [_make_item("uuid-1")], now=NOW)
    assert not old_file.exists()

    # Recrée le fichier simulant un crash puis réécriture
    old_file.write_text("")
    # Deuxième append le même jour : purge PAS re-déclenchée
    arc.append_items("AAPL", [_make_item("uuid-2")], now=NOW)
    assert old_file.exists(), "Purge ne doit pas être re-déclenchée le même jour"


def test_purge_ignores_malformed_filenames(tmp_path):
    """Les fichiers au nom non conforme (non YYYY-MM-DD.jsonl) sont ignorés."""
    base = tmp_path / "news_items"
    base.mkdir()

    for name in ("README.jsonl", "archive-extra.jsonl", "20260101.jsonl"):
        (base / name).write_text("")

    arc = nf.NewsItemsArchive(base)
    arc.append_items("AAPL", [_make_item("uuid-m")], now=NOW)

    # Les fichiers malformés doivent survivre
    for name in ("README.jsonl", "archive-extra.jsonl", "20260101.jsonl"):
        assert (base / name).exists(), f"{name} ne devrait pas être purgé"


# ---------------------------------------------------------------------------
# _item_uuid — extraction depuis différents formats yfinance
# ---------------------------------------------------------------------------


def test_item_uuid_from_top_level(tmp_path):
    """UUID extrait depuis item['uuid'] (format standard)."""
    assert nf._item_uuid({"uuid": "abc-123"}) == "abc-123"


def test_item_uuid_from_id_fallback():
    """UUID extrait depuis item['id'] si pas de 'uuid'."""
    assert nf._item_uuid({"id": "id-456"}) == "id-456"


def test_item_uuid_from_content():
    """UUID extrait depuis content.id si absent au niveau racine."""
    assert nf._item_uuid({"content": {"id": "content-789"}}) == "content-789"


def test_item_uuid_none_when_absent():
    """None si aucun identifiant présent."""
    assert nf._item_uuid({"title": "No id here"}) is None


# ---------------------------------------------------------------------------
# RawNews.items — rétrocompatibilité
# ---------------------------------------------------------------------------


def test_raw_news_items_default_empty():
    """RawNews construit sans `items` a items=() par défaut (compat existante)."""
    raw = nf.RawNews(mapped=True, earnings_dates=(), news_count=1)
    assert raw.items == ()


def test_raw_news_items_excluded_from_equality():
    """items est exclu du compare : deux RawNews identiques sauf items sont égaux."""
    raw1 = nf.RawNews(mapped=True, earnings_dates=(), news_count=1, items=({"uuid": "x"},))
    raw2 = nf.RawNews(mapped=True, earnings_dates=(), news_count=1, items=())
    assert raw1 == raw2


def test_raw_news_hashable_with_items():
    """RawNews avec items de dicts est hashable (items exclus du hash)."""
    raw = nf.RawNews(mapped=True, earnings_dates=(), news_count=1, items=({"uuid": "x"},))
    _ = hash(raw)  # ne doit pas lever TypeError
