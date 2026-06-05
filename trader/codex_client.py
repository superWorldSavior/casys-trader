"""codex_client — appel programmatique à Codex (le brain décideur).

Invoque Codex en headless via `acpx --format quiet exec` (codex est l'agent par
défaut d'acpx). On lui passe le contexte + le mandat, et on force une sortie JSON
structurée (le schéma de décision). Aucune stratégie ici : juste le transport vers
le LLM et la validation stricte de sa réponse.

Décision PURE : on coupe les outils (`--allowed-tools ""`) et le terminal
(`--no-terminal`) — le brain ne fait que raisonner sur le contexte fourni, il ne
touche ni au FS ni au shell. `exec` = session temporaire (pas d'état partagé) ->
déterministe. L'état évolutif de l'agent vit dans memory.md, passé dans le prompt.

Fail-safe : toute erreur (timeout, binaire absent, JSON invalide) -> décision
HOLD. On ne trade JAMAIS sur une réponse douteuse.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal

Action = Literal["BUY", "SELL", "HOLD"]

# Modèle du brain runtime : Codex Spark 5.3 (famille rapide) à effort xhigh.
# Spark = faible latence, adapté à un agent en veille qui décide à chaque réveil.
DEFAULT_MODEL = "gpt-5.3-codex-spark[xhigh]"

_DECISION_KEYS = {"symbol", "action", "quantity", "confidence", "rationale"}


@dataclass(frozen=True)
class Decision:
    symbol: str
    action: Action
    quantity: float          # nombre d'unités ; ignoré si HOLD
    confidence: float        # 0..1
    rationale: str

    @staticmethod
    def hold(symbol: str, reason: str) -> "Decision":
        return Decision(symbol=symbol, action="HOLD", quantity=0.0, confidence=0.0, rationale=reason)


_OUTPUT_CONTRACT = (
    "Réponds UNIQUEMENT par un objet JSON valide, sans texte autour, de la forme:\n"
    '{"symbol": "<SYM>", "action": "BUY|SELL|HOLD", "quantity": <number>, '
    '"confidence": <0..1>, "rationale": "<court>"}\n'
    "Si tu n'es pas sûr, renvoie action=HOLD."
)


def build_prompt(*, mandate: str, memory: str, context: dict) -> str:
    """Assemble le prompt. Le COMPORTEMENT vit dans `mandate`/`memory` (boucle 1),
    pas en dur ici."""
    return (
        "Tu es l'agent décideur d'un système de trading paper.\n\n"
        f"# Mandat\n{mandate}\n\n"
        f"# Mémoire / stratégie\n{memory}\n\n"
        f"# Contexte marché et portefeuille (JSON)\n{json.dumps(context, ensure_ascii=False)}\n\n"
        f"# Contrat de sortie\n{_OUTPUT_CONTRACT}\n"
    )


def _extract_json(text: str) -> dict:
    """Récupère le 1er objet JSON du texte (Codex peut entourer de prose)."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("aucun objet JSON trouvé")
    return json.loads(text[start : end + 1])


def parse_decision(raw_text: str, symbol: str) -> Decision:
    data = _extract_json(raw_text)
    missing = _DECISION_KEYS - data.keys()
    if missing:
        raise ValueError(f"clés manquantes: {missing}")
    action = str(data["action"]).upper()
    if action not in ("BUY", "SELL", "HOLD"):
        raise ValueError(f"action invalide: {action}")
    return Decision(
        symbol=str(data["symbol"]),
        action=action,  # type: ignore[arg-type]
        quantity=float(data["quantity"]),
        confidence=float(data["confidence"]),
        rationale=str(data["rationale"]),
    )


def build_command(prompt: str, *, acpx_bin: str, model: str, timeout_s: int) -> list[str]:
    """Commande acpx pour une décision pure (codex, sans outils, sortie texte brute)."""
    return [
        acpx_bin,
        "--format", "quiet",            # texte final de l'assistant uniquement
        "--allowed-tools", "",          # décision pure : aucun outil
        "--no-terminal",                # pas de capacité terminal (sandboxé)
        "--non-interactive-permissions", "deny",  # pas de TTY dans le daemon
        "--model", model,               # Codex Spark 5.3 xhigh (rapide)
        "--timeout", str(timeout_s),
        "exec",                         # one-shot, session temporaire -> déterministe
        prompt,
    ]


def decide(
    *,
    symbol: str,
    mandate: str,
    memory: str,
    context: dict,
    acpx_bin: str = "acpx",
    model: str = DEFAULT_MODEL,
    timeout_s: int = 120,
) -> Decision:
    """Appelle Codex (via acpx) et renvoie une Decision validée. Tout échec -> HOLD."""
    if shutil.which(acpx_bin) is None:
        return Decision.hold(symbol, f"acpx_unavailable: binaire '{acpx_bin}' introuvable")

    prompt = build_prompt(mandate=mandate, memory=memory, context=context)
    try:
        proc = subprocess.run(
            build_command(prompt, acpx_bin=acpx_bin, model=model, timeout_s=timeout_s),
            capture_output=True,
            text=True,
            timeout=timeout_s + 15,  # backstop au-dessus du timeout acpx
        )
    except subprocess.TimeoutExpired:
        return Decision.hold(symbol, f"codex_timeout: > {timeout_s}s")
    except Exception as e:  # noqa: BLE001 — frontière externe
        return Decision.hold(symbol, f"codex_exec_failed: {e}")

    if proc.returncode != 0:
        return Decision.hold(symbol, f"codex_nonzero_exit: {proc.returncode} | {proc.stderr[:200]}")

    try:
        return parse_decision(proc.stdout, symbol)
    except Exception as e:  # noqa: BLE001
        return Decision.hold(symbol, f"codex_bad_output: {e}")
