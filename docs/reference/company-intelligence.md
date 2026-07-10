# Company intelligence

> **Type** : référence opérateur.
> **Statut** : implémenté le 2026-07-10, fail-open et sans autorité de trading.

## Chaîne de données

```text
SEC / FinMind-MOPS / filings.xbrl.org / yfinance
  -> CompanyEvidenceSnapshot
  -> state/fundamental_items/<symbol-hash>.jsonl
  -> agent company-micro
  -> state/company_intelligence/{history,current}/
  -> analyste news + agent Univers
  -> mandat Univers préparé puis activé
  -> tranche symbole du trader (observe)
  -> decisions.jsonl (refs réellement vues)
```

L'analyse est longitudinale par symbole. Elle n'est pas recréée « par journée » :
l'historique canonique reste append-only et la projection `current` est
reconstructible. Le fingerprint ignore l'heure d'une simple sonde source ; une
analyse n'est réémise que si l'évidence normalisée change.

## Sources

| Venue | Source officielle | Fallback |
|---|---|---|
| US | SEC EDGAR Company Facts (`CASYS_SEC_USER_AGENT` requis) | yfinance |
| TW | FinMind `TaiwanStockFinancialStatements` / MOPS (`FINMIND_TOKEN` optionnel) | yfinance |
| EU | API publique `filings.xbrl.org` pour les LEI déclarés dans `config/company_entities.yaml` | yfinance |

Pour toutes les venues, l'archive locale `state/news_items` ajoute les nouvelles
entreprise récentes déjà collectées. Elle ne lance aucun nouveau scan web et
reste strictement filtrée par symbole.

Une panne de provider est isolée et enregistrée dans la couverture ; elle
n'efface pas les autres sources. L'identité, la période, le type d'évidence et
les `source_refs` sont conservés. Les documents bruts ne sont jamais injectés
dans les prompts Univers ou trader.

## Runtime et file dédiée

Le runtime utilise exclusivement `state/company_research_tasks.db`, séparé des
ledgers `decide` et `execute` :

- `kind=company_micro` ;
- `partition_key=symbol` ;
- `dedup_key=symbol:input_signature:depth` ;
- ressource `company-research`, concurrence configurée par
  `research_concurrency` ;
- retries/backoff et statuts `pending/running/done/dead` du `TaskLedger`.

La collecte et l'analyse sont lancées dans des threads de fond. Le daemon ne
les attend jamais. Un nouveau brief retrigger/coalesce la préparation Univers.
Au boot et après chaque cycle, le scope courant regroupe candidats persistés,
sticky et univers actif ; aucun nombre maximal de candidats n'est appliqué.

Kill switch : `CASYS_COMPANY_MICRO_ANALYST_ENABLED=0`.

## CLI

```bash
casys-trader company-intelligence refresh --scope current --wait
casys-trader company-intelligence refresh --scope active --depth deep --wait
casys-trader company-intelligence refresh --symbol AAPL --symbol 2330.TW --wait
casys-trader company-intelligence status
```

Sans scope candidat disponible, `--scope current` utilise l'univers actif et
signale `candidate_coverage_pending=true`.

## Autorités

- L'analyste micro décrit l'entreprise et une thèse sourcée. Il ne sélectionne
  aucun symbole et ne produit ni ordre, ni quantité, ni stop, ni sizing.
- L'analyste news peut comparer une news à une ancre micro, sans réécrire cette
  ancre.
- L'agent Univers reste le seul propriétaire de `selected_hotlist`.
- Un mandat Univers `prepared` n'est jamais visible au trader. Seul le mandat
  réellement activé par la rotation est projeté.
- Le trader reçoit uniquement la tranche du symbole courant, en mode `observe`.
  `allowed_sides` ne bloque aucune décision ; le RiskGate reste indépendant.
- Un sticky sans mandat reçoit `managed_existing/sticky_unmandated` et garde
  toujours la possibilité de réduire ou fermer.

## Configuration et observabilité

`config/company_intelligence.yaml` contrôle la concurrence et les modes
`universe_company_context_mode` / `trader_company_context_mode`. Le cockpit
Univers expose la couverture micro, les statuts de queue et le mandat activé.
Chaque run Univers fige le hash, les compteurs et les références de briefs
consommées. Chaque décision persiste `company_brief_refs` et `mandate_ref`.

L'API ESEF utilisée est l'endpoint public `/api/filings` avec filtre d'entité ;
les LEI ne sont jamais devinés et doivent être déclarés explicitement.
