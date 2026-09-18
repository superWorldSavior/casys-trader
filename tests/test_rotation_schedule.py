"""Tests TDD pour trader/rotation_schedule.py — gate de déclenchement rotation."""


import trader.market.rotation.schedule as rotation_schedule
from trader.market.rotation.schedule import closed_sessions_since, load_sessions

_STANDARD_SESSIONS = {
    "TW": {"open": "01:00", "close": "05:30"},
    "EU": {"open": "07:00", "close": "15:30"},
    "US": {"open": "13:30", "close": "20:00"},
}


# ---------------------------------------------------------------------------
# load_sessions
# ---------------------------------------------------------------------------


class TestLoadSessions:
    def test_fichier_absent_retourne_defaut(self, tmp_path):
        """config/sessions.yaml absent → défaut standard."""
        result = load_sessions(str(tmp_path))
        assert result == _STANDARD_SESSIONS

    def test_lit_fichier_yaml(self, tmp_path):
        """config/sessions.yaml présent → lit les valeurs."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "sessions.yaml").write_text(
            "TW: {open: '01:15', close: '06:00'}\n"
            "EU: {open: '07:15', close: '16:00'}\n"
            "US: {open: '13:45', close: '21:00'}\n"
        )
        result = load_sessions(str(tmp_path))
        assert result == {
            "TW": {"open": "01:15", "close": "06:00"},
            "EU": {"open": "07:15", "close": "16:00"},
            "US": {"open": "13:45", "close": "21:00"},
        }

    def test_fichier_corrompu_retourne_defaut(self, tmp_path):
        """YAML corrompu → défaut, pas d'exception."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "sessions.yaml").write_text("{ invalid: yaml: :\n")
        result = load_sessions(str(tmp_path))
        assert result == _STANDARD_SESSIONS


# ---------------------------------------------------------------------------
# closed_sessions_since
# ---------------------------------------------------------------------------


class TestClosedSessionsSince:
    def test_eu_dans_fenetre(self):
        """EU 15:30 tombe entre last=06:00 et now=16:00 → ["EU"]."""
        result = closed_sessions_since(
            "2026-06-15T16:00:00+00:00",
            "2026-06-15T06:00:00+00:00",
            _STANDARD_SESSIONS,
        )
        assert result == ["EU"]

    def test_us_dans_fenetre(self):
        """US 20:00 tombe entre last=16:00 et now=21:00 → ["US"]."""
        result = closed_sessions_since(
            "2026-06-15T21:00:00+00:00",
            "2026-06-15T16:00:00+00:00",
            _STANDARD_SESSIONS,
        )
        assert result == ["US"]

    def test_tw_et_eu_dans_fenetre(self):
        """TW et EU tombent entre last=00:00 et now=16:00 → ["EU","TW"] triés."""
        result = closed_sessions_since(
            "2026-06-15T16:00:00+00:00",
            "2026-06-15T00:00:00+00:00",
            _STANDARD_SESSIONS,
        )
        assert result == ["EU", "TW"]

    def test_aucune_session_dans_fenetre(self):
        """Aucune clôture dans la fenêtre → []."""
        result = closed_sessions_since(
            "2026-06-15T10:00:00+00:00",
            "2026-06-15T06:00:00+00:00",
            _STANDARD_SESSIONS,
        )
        assert result == []

    def test_last_none_bootstrap_eu_et_tw(self):
        """last=None, now=16:00 → toutes les clôtures passées du jour : ["EU","TW"]."""
        result = closed_sessions_since(
            "2026-06-15T16:00:00+00:00",
            None,
            _STANDARD_SESSIONS,
        )
        assert result == ["EU", "TW"]

    def test_last_vide_string_bootstrap(self):
        """last="" (string vide) ≡ None → bootstrap."""
        result = closed_sessions_since(
            "2026-06-15T16:00:00+00:00",
            "",
            _STANDARD_SESSIONS,
        )
        assert result == ["EU", "TW"]

    def test_last_none_now_avant_premiere_cloture(self):
        """last=None, now=04:00 → aucune clôture passée."""
        result = closed_sessions_since(
            "2026-06-15T04:00:00+00:00",
            None,
            _STANDARD_SESSIONS,
        )
        assert result == []

    def test_passage_minuit_clotures_veille(self):
        """now=01:00 le 16, last=19:00 le 15 → US 20:00 du 15 est couverte."""
        result = closed_sessions_since(
            "2026-06-16T01:00:00+00:00",
            "2026-06-15T19:00:00+00:00",
            _STANDARD_SESSIONS,
        )
        assert result == ["US"]

    def test_passage_minuit_plusieurs_jours(self):
        """now=06:00 le 16, last=18:00 le 15 → US 20:00 le 15 + TW 05:30 le 16."""
        result = closed_sessions_since(
            "2026-06-16T06:00:00+00:00",
            "2026-06-15T18:00:00+00:00",
            _STANDARD_SESSIONS,
        )
        assert result == ["TW", "US"]

    def test_exact_sur_borne_cloture(self):
        """now == heure exacte de clôture EU → EU inclus dans la fenêtre."""
        result = closed_sessions_since(
            "2026-06-15T15:30:00+00:00",
            "2026-06-15T06:00:00+00:00",
            _STANDARD_SESSIONS,
        )
        assert "EU" in result

    def test_retour_trie(self):
        """Résultat toujours trié alphabétiquement."""
        result = closed_sessions_since(
            "2026-06-15T21:00:00+00:00",
            "2026-06-15T00:00:00+00:00",
            _STANDARD_SESSIONS,
        )
        assert result == sorted(result)


# ---------------------------------------------------------------------------
# open_venues
# ---------------------------------------------------------------------------


class TestOpenVenues:
    def test_lundi_0300_fx_et_tw(self):
        result = rotation_schedule.open_venues(
            "2026-06-15T03:00:00+00:00",
            _STANDARD_SESSIONS,
        )
        assert result == ["FX", "TW"]

    def test_lundi_1400_overlap_eu_fx_us(self):
        result = rotation_schedule.open_venues(
            "2026-06-15T14:00:00+00:00",
            _STANDARD_SESSIONS,
        )
        assert result == ["EU", "FX", "US"]

    def test_lundi_2100_fx_seul(self):
        result = rotation_schedule.open_venues(
            "2026-06-15T21:00:00+00:00",
            _STANDARD_SESSIONS,
        )
        assert result == ["FX"]

    def test_samedi_fx_ferme(self):
        result = rotation_schedule.open_venues(
            "2026-06-20T03:00:00+00:00",
            _STANDARD_SESSIONS,
        )
        assert result == []


# ---------------------------------------------------------------------------
# preopen_venues (A1)
# ---------------------------------------------------------------------------

_SESS = {
    "TW": {"open": "01:00", "close": "05:30"},
    "EU": {"open": "07:00", "close": "15:30"},
    "US": {"open": "13:30", "close": "20:00"},
}


class TestPreopenVenues:
    def test_preopen_inclut_venue_dans_la_fenetre(self):
        """00:30 UTC mardi, fenêtre 90 min → TW (ouvre 01:00) est en pré-open."""
        from trader.market.rotation.schedule import preopen_venues

        assert preopen_venues("2026-06-16T00:30:00+00:00", _SESS, window_minutes=90) == ["TW"]

    def test_preopen_exclut_venue_hors_fenetre(self):
        """23:00 UTC → prochaine ouverture TW (01:00 lendemain) à >90 min → vide."""
        from trader.market.rotation.schedule import preopen_venues

        assert preopen_venues("2026-06-16T23:00:00+00:00", _SESS, window_minutes=90) == []

    def test_preopen_exclut_venue_deja_ouverte(self):
        """02:00 UTC → TW déjà ouverte (pas pré-open), EU/US trop loin."""
        from trader.market.rotation.schedule import preopen_venues

        assert preopen_venues("2026-06-16T02:00:00+00:00", _SESS, window_minutes=90) == []

    def test_preopen_weekend_pas_de_preopen_actions(self):
        """Samedi → aucune venue actions en pré-open (le lundi est à >fenêtre)."""
        from trader.market.rotation.schedule import preopen_venues

        assert preopen_venues("2026-06-20T00:30:00+00:00", _SESS, window_minutes=90) == []

    def test_preopen_exclut_fx_par_construction(self):
        """FX = 24/5, pas de gong → jamais pré-open, MÊME présent dans sessions.

        Repro du finding Codex : dimanche soir, open_venues=[] (week-end) donc FX
        absent d'open_now ; sans garde explicite, FX remonterait en pré-open de sa
        pseudo-ouverture du lundi. La garde `venue == "FX"` doit l'exclure.
        """
        from trader.market.rotation.schedule import preopen_venues

        sess = {"FX": {"open": "00:00", "close": "22:00"}}
        assert preopen_venues("2026-06-21T23:30:00+00:00", sess, window_minutes=90) == []


# ---------------------------------------------------------------------------
# analyzable_venues (A2)
# ---------------------------------------------------------------------------


class TestAnalyzableVenues:
    def test_analyzable_union_ouvertes_et_preopen(self):
        """00:30 UTC mardi : FX ouvert (24/5) + TW en pré-open."""
        from trader.market.rotation.schedule import analyzable_venues

        result = analyzable_venues("2026-06-16T00:30:00+00:00", _SESS, preopen_window_minutes=90)
        assert result == ["FX", "TW"]

    def test_analyzable_egal_open_quand_aucun_preopen(self):
        """02:00 UTC : TW + FX ouverts, aucun pré-open."""
        from trader.market.rotation.schedule import analyzable_venues

        result = analyzable_venues("2026-06-16T02:00:00+00:00", _SESS, preopen_window_minutes=90)
        assert result == ["FX", "TW"]
