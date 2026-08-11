# Référence — Company intelligence

> **Type** : Reference (Diátaxis).
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

Pour SEC EDGAR, ajouter dans `.env` un contact réel, puis redémarrer le daemon :

```dotenv
CASYS_SEC_USER_AGENT=Casys Trader admin@example.com
```

Ce flux public ne demande ni clé API ni token EDGAR Next. Le User-Agent sert à
identifier l'automatisation et doit contenir une adresse à laquelle la SEC peut
contacter l'opérateur.

Une panne de provider est isolée et enregistrée dans la couverture ; elle
n'efface pas les autres sources. L'identité, la période, le type d'évidence et
les `source_refs` sont conservés. Les documents bruts ne sont jamais injectés
dans les prompts Univers ou trader.

## Runtime et file dédiée

Le runtime utilise exclusivement `state/company_research_tasks.db`, séparé des
ledgers `decide` et `execute` :

- `kind=company_micro` ;
- `partition_key=symbol` ;
- `dedup_key=symbol:input_signature:depth:date` ;
- ressource `company-research`, concurrence configurée par
  `research_concurrency` ;
- retries/backoff et statuts `pending/running/done/dead` du `TaskLedger`.

La collecte et l'analyse sont lancées dans des threads de fond. Le daemon ne
les attend jamais. Au boot et après chaque cycle, le scope courant regroupe
candidats persistés, sticky et univers actif ; aucun nombre maximal de
candidats n'est appliqué.

Une analyse déjà exploitable est rafraîchie au plus toutes les 24 h. Un nouveau
snapshot fondamental, profil ou événement earnings la rend immédiatement due ;
les news seules ne changent pas cette signature fondamentale. La fenêtre
pré-open rend également le brief éligible. Une simple sonde inchangée est
retournée dans `skipped` mais n'écrit plus une ligne durable à chaque tick.

Une tâche a six tentatives totales. Après les échecs 1 à 5, la file planifie
respectivement 30 min, 1 h, 2 h, 4 h puis 6 h ; le sixième échec passe en
`dead`. Chaque tentative matérielle reste appendue dans
`state/company_analysis_runs/`. La projection `latest` conserve le dernier
succès utilisable et ajoute la panne courante sous `latest_failure` avec
tentative, délai et `next_retry_at` ; un succès ultérieur efface ce marqueur.

### Politique de propagation courante

Dans le daemon, un nouveau brief micro écrit par son worker signale uniquement
sa venue de cotation (`TW`, `EU` ou `US`). Les signaux d'une même vague sont
coalescés par un debounce trailing de 120 s avant une éventuelle nouvelle
préparation Univers ; ils ne réveillent pas les deux autres rapports régionaux.
Le runtime construit par la CLI n'installe pas ce callback : un refresh manuel
écrit bien le brief partagé, mais la CLI seule ne déclenche aucun passage
Univers. Le brief sera consommé lors d'un passage autrement éligible, par
exemple un nouveau scope ou brief macro, ou une préparation pas encore faite.
Cette nouvelle préparation n'est autorisée que tant que le
`candidate_scope_id` courant n'a pas été activé. Après l'activation réussie du
scope exact, les nouveaux micros restent disponibles comme provenance et pour
le prochain scope, mais ne recomposent ni la hotlist ni le mandat actifs.

Le rapport macro/news régional conserve lui aussi les références micro exactes,
sans faire de chaque brief entreprise un déclencheur immédiat. Il consomme les
derniers micros au prochain refresh éligible après le cooldown de succès de 4 h,
lors d'un refresh manuel `--force`, ou immédiatement lorsqu'un nouveau scope
candidat exige un brief exact-scope.

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
Univers expose la couverture micro, les statuts de queue et le mandat activé ;
la page Reports montre le dernier brief et, séparément, la dernière panne.
Chaque run Univers fige le hash, les compteurs et les références de briefs
consommées. Chaque décision persiste `company_brief_refs` et `mandate_ref`.

L'API ESEF utilisée est l'endpoint public `/api/filings` avec filtre d'entité ;
les LEI ne sont jamais devinés et doivent être déclarés explicitement.

## Voir aussi

- [How-to : rafraîchir et diagnostiquer les rapports](../how-to/refresh-and-diagnose-reports.md)
- [News et analyste macro](news.md) · [gestion d'univers](universe-rotation.md)
