# Plan — fill plan effects

## Objectif

Réduire le bloc post-fill de `trader/runtime/daemon.py` en sortant les effets
sur `TradePlan` vers un service applicatif testable.

## Tranche

- Ajouter `trader/application/fill_plan_effects.py`.
- Déplacer les effets déterministes après fill :
  close sur `CLOSE` / `FLIP`, sync quantité sur `REDUCE`, création/snapshot
  de plan pour `OPEN_LONG` / `OPEN_SHORT` / `SCALE_IN` / `FLIP`.
- Préserver le mode queue canon : pas de double close/upsert quand l'UoW a déjà
  appliqué `symbol_to_close` / `plan_to_upsert`.
- Garder dans le daemon le contexte runtime concret et le scheduling post-entry.
- Ajouter un garde de layout empêchant le retour du mapping plan-store inline
  dans le bloc post-fill du daemon.

## Hors périmètre

- Ne pas déplacer le `RiskGate`.
- Ne pas changer le fallback broker synchrone.
- Ne pas changer la politique de réveil post-entry.
