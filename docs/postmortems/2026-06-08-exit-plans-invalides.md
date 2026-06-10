# Post-mortem — 7 exit_plans invalides émis par le LLM (05-08 juin 2026)

**Statut : RÉSOLU PAR DESIGN** — commit `a3357d5` « exposer le schéma
exit_plan complet dans le contrat batch ». **Zéro occurrence depuis**
(vérifié sur ~950 décisions daemon du 08 au 10/06). Ce document existe pour
que les stats incluant le 05-08/06 ne fassent pas conclure à un problème
actif.

## Les faits

7 décisions d'ouverture rejetées par le parseur d'exit_plan (l'ordre n'a
jamais atteint le risk gate — fail-safe correct) :

| Date (UTC) | Symbole | Code de rejet |
|---|---|---|
| 05/06 15:07 | SPY | `trailing_stop_trail_value_required` |
| 05/06 15:08 | QQQ | `take_profits_0_must_be_object` |
| 05/06 19:14 | USDCHF=X | `trailing_stop_type_unsupported` |
| 05/06 20:45 | EWT | `trailing_stop_type_unsupported` |
| 06/06 02:28 | SPY | `trailing_stop_type_unsupported` |
| 06/06 02:58 | SPY | `trailing_stop_type_unsupported` |
| 08/06 07:32 | ^FCHI | `hard_stop_price_required` |

Tous sur les commits `e105c3b`/`c6a9f83` — **avant** `a3357d5`.

## Cause racine

Le contrat de sortie montré au LLM ne décrivait pas le schéma complet de
`exit_plan` (types de trailing supportés, structure des take_profits,
champs requis). Le modèle improvisait des variantes plausibles
(`trailing_stop` en type non supporté, TP en nombre nu au lieu d'objet) que
le parseur — strict, à juste titre — rejetait. Friction d'interface
classique (AX) : un agent ne peut pas respecter un contrat qu'on ne lui
montre pas.

## Le fix (déjà livré à l'époque)

`a3357d5` expose le schéma exit_plan complet dans le contrat batch. Effet
mesuré : 7 rejets sur 3 jours avant → 0 rejet depuis (3 jours, ~950
décisions, dont des dizaines d'exit_plans valides ayant produit des trade
plans réels).

## Limite d'observabilité de l'époque (comblée depuis)

Le payload fautif n'avait pas été persisté — on connaît le code de rejet
mais pas le JSON exact émis. Depuis `5ce1746` (persistance des trade plans)
et le pipeline de traçabilité courant, un récidive serait entièrement
reconstituable.

## Surveillance

`python -m trader.tool_usage` liste ces rejets dans le détail des
exclusions — si `invalid_exit_plan:*` réapparaît sur du code ≥ `a3357d5`,
rouvrir ce dossier (le contrat aura dérivé du parseur).
