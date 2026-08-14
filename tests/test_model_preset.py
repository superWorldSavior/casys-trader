"""Invariants de la bascule de presets modèles (scripts/model_preset.py).

Le piège que ces tests verrouillent : ``trader.agent.llm.load_dotenv`` garde la
PREMIÈRE occurrence d'une clé (``if override or key not in os.environ``). Une
assignation gérée laissée hors du bloc généré gagnerait donc silencieusement sur
le preset — le daemon tournerait sur un modèle que le ``.env`` semble avoir
changé. La bascule doit retirer ces lignes, pas seulement écrire le bloc.
"""

from __future__ import annotations

import pytest

from scripts import model_preset


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(model_preset, "ENV_PATH", path)
    return path


def _apply(name: str, *, write: bool) -> int:
    argv = ["apply", name] + (["--write"] if write else [])
    return model_preset.main(argv)


def test_dry_run_par_defaut_n_ecrit_rien(env_file) -> None:
    env_file.write_text("TRADER_MODEL=kimi-code/k3\n", encoding="utf-8")

    assert _apply("codex-luna-medium", write=False) == 0
    assert env_file.read_text(encoding="utf-8") == "TRADER_MODEL=kimi-code/k3\n"


def test_apply_retire_les_assignations_gerees_hors_bloc(env_file) -> None:
    env_file.write_text(
        "CASYS_QUEUE_DECIDE_ENABLED=1\nTRADER_MODEL=kimi-code/k3\nTRADER_ACPX_AGENT=kimi\n",
        encoding="utf-8",
    )

    assert _apply("codex-luna-medium", write=True) == 0

    text = env_file.read_text(encoding="utf-8")
    assert "TRADER_MODEL=kimi-code/k3" not in text
    assert "TRADER_MODEL=gpt-5.6-luna" in text
    # une seule assignation par clé gérée, sinon la première gagnerait au boot
    assert text.count("TRADER_MODEL=") == 1
    assert text.count("TRADER_ACPX_AGENT=") == 1
    # les clés non gérées ne sont jamais touchées
    assert "CASYS_QUEUE_DECIDE_ENABLED=1" in text


def test_bascule_retire_les_cles_absentes_du_preset_cible(env_file) -> None:
    _apply("codex-luna-medium", write=True)
    assert "TRADER_REASONING_EFFORT=medium" in env_file.read_text(encoding="utf-8")

    _apply("kimi", write=True)

    text = env_file.read_text(encoding="utf-8")
    assert "TRADER_REASONING_EFFORT=" not in text
    assert "TRADER_UNIVERSE_MODEL=kimi-code/k3" in text


def test_repo_est_substitue_en_chemin_absolu(env_file) -> None:
    _apply("kimi", write=True)

    text = env_file.read_text(encoding="utf-8")
    assert "${REPO}" not in text
    assert f"KIMI_CODE_HOME={model_preset.REPO_ROOT}/ops/kimi-home" in text


def test_preset_grok_substitue_grok_home(env_file) -> None:
    _apply("grok-4.6-low", write=True)

    text = env_file.read_text(encoding="utf-8")
    assert "${REPO}" not in text
    assert f"GROK_HOME={model_preset.REPO_ROOT}/ops/grok-home" in text
    assert f"TRADER_GROK_HOME={model_preset.REPO_ROOT}/ops/grok-home" in text
    assert f"TRADER_CONSOLIDATOR_GROK_HOME={model_preset.REPO_ROOT}/ops/grok-home" in text
    assert "TRADER_ACPX_AGENT=grok-build" in text
    assert "TRADER_MODEL=grok-4.6" in text
    assert "TRADER_REASONING_EFFORT=" not in text


def test_preset_grok_brain_low_analystes_medium(env_file) -> None:
    _apply("grok", write=True)

    text = env_file.read_text(encoding="utf-8")
    root = model_preset.REPO_ROOT
    assert "${REPO}" not in text
    assert f"TRADER_GROK_HOME={root}/ops/grok-home" in text
    assert f"TRADER_CONSOLIDATOR_GROK_HOME={root}/ops/grok-home-medium" in text
    assert f"TRADER_UNIVERSE_GROK_HOME={root}/ops/grok-home-medium" in text
    assert f"TRADER_COMPANY_MICRO_GROK_HOME={root}/ops/grok-home-medium" in text
    assert "TRADER_ACPX_AGENT=grok-build" in text
    assert "TRADER_MODEL=grok-4.6" in text
    assert "TRADER_REASONING_EFFORT=" not in text
    assert "KIMI_CODE_HOME=" not in text


def test_bascule_kimi_vers_grok_retire_kimi_et_pose_les_homes(env_file) -> None:
    _apply("kimi", write=True)
    assert "KIMI_CODE_HOME=" in env_file.read_text(encoding="utf-8")

    _apply("grok", write=True)

    text = env_file.read_text(encoding="utf-8")
    assert "KIMI_CODE_HOME=" not in text
    assert "casys:model-preset=grok" in text
    assert "TRADER_GROK_HOME=" in text
    assert "grok-home-medium" in text


def test_apply_est_idempotent(env_file) -> None:
    _apply("codex-luna-medium", write=True)
    once = env_file.read_text(encoding="utf-8")

    _apply("codex-luna-medium", write=True)

    assert env_file.read_text(encoding="utf-8") == once


def test_apply_ecrit_une_sauvegarde(env_file) -> None:
    env_file.write_text("TRADER_MODEL=kimi-code/k3\n", encoding="utf-8")

    _apply("codex-luna-medium", write=True)

    assert (env_file.parent / ".env.bak").read_text(encoding="utf-8") == "TRADER_MODEL=kimi-code/k3\n"


def test_bloc_non_termine_refuse_d_ecrire(env_file) -> None:
    env_file.write_text(
        "# >>> casys:model-preset=kimi >>>\nTRADER_MODEL=kimi-code/k3\n", encoding="utf-8"
    )

    with pytest.raises(model_preset.PresetError) as exc:
        model_preset.plan_apply("codex-luna-medium")

    assert exc.value.code == "block_unterminated"


def test_preset_inconnu_remonte_un_code(env_file) -> None:
    with pytest.raises(model_preset.PresetError) as exc:
        model_preset.load_preset("nexistepas")

    assert exc.value.code == "preset_not_found"


def test_show_reconnait_le_preset_actif(env_file, capsys) -> None:
    _apply("kimi", write=True)
    capsys.readouterr()

    model_preset.main(["show"])

    assert "preset actif : kimi" in capsys.readouterr().out


def test_les_presets_versionnes_couvrent_les_cinq_roles() -> None:
    """Un preset qui oublie un rôle le laisse sur le défaut code, en silence."""

    roles = ("", "CONSOLIDATOR_", "UNIVERSE_", "COMPANY_MICRO_", "NEWS_MACRO_")
    for name in model_preset.available_presets():
        pairs = model_preset.load_preset(name)
        for role in roles:
            assert f"TRADER_{role}MODEL" in pairs, f"{name} n'affecte pas le rôle {role or 'brain'}"
            assert f"TRADER_{role}ACPX_AGENT" in pairs, f"{name} laisse l'agent du rôle {role or 'brain'}"
