# Sessions acpx persistantes pour le runtime trader

## Objectif

Le runtime peut aujourd'hui appeler Codex via `acpx exec`, donc chaque décision
part d'une session temporaire. C'est simple et déterministe, mais l'agent ne garde
pas de continuité conversationnelle entre deux réveils. L'objectif est d'ajouter
un mode optionnel de session acpx persistante pour le brain runtime, sans déplacer
la source de vérité hors de `state/`.

Le besoin immédiat est une session globale `trader-runtime`, pas une session par
symbole. Une session globale garde la lecture cross-asset et évite de créer vingt
mini-agents qui pourraient diverger sur le même portefeuille.

## Doctrine

La session acpx est une mémoire de travail, jamais une source de vérité.

À chaque réveil, le daemon reconstruit un contexte canonique depuis les fichiers
runtime et les données marché fraîches : portefeuille, positions, plans ouverts,
cockpit cross-asset, symboles stale, KPIs, triggers et derniers événements utiles.
Le prompt doit rappeler que ce contexte écrase toute croyance précédente de la
conversation.

Si les données marché d'un symbole sont stale, le modèle n'est pas réveillé pour
ce symbole. Ce garde-fou reste en amont de toute session persistante.

## Architecture

Le daemon continue d'appeler `codex_client.decide(...)`. Le choix `exec` ou
session persistante reste dans la couche transport LLM.

Composants :

- `trader.llm.AcpxBackend` : conserve le mode actuel `acpx ... exec`.
- Nouveau backend ou option, par exemple `AcpxSessionBackend`, pour appeler
  `acpx ... -s trader-runtime prompt`.
- `trader.codex_client` : inchangé dans son contrat métier, il construit le prompt,
  parse strictement la réponse JSON et transforme toute erreur en HOLD.
- `trader.daemon` : reçoit seulement les métadonnées LLM/session et les écrit dans
  les reports. Il ne connaît pas la mécanique acpx.

Configuration proposée :

```bash
TRADER_ACPX_SESSION_MODE=stateless|persistent
TRADER_ACPX_SESSION_NAME=trader-runtime
```

Le défaut reste `stateless` pour préserver le comportement actuel.

## Data Flow

Mode stateless :

```text
daemon -> codex_client -> llm.AcpxBackend -> acpx exec -> JSON Decision
```

Mode persistent :

```text
daemon -> codex_client -> llm.AcpxSessionBackend
       -> ensure session trader-runtime
       -> acpx -s trader-runtime prompt
       -> JSON Decision
```

Avant chaque prompt, le contexte contient un bloc court de priorité :

```text
Le contexte JSON ci-dessous est la vérité canonique du runtime.
Il remplace toute mémoire conversationnelle contradictoire.
Ne décide jamais contre le portefeuille, les plans, les prix frais ou les
symboles stale fournis ici.
```

## Session Lifecycle

Au premier appel en mode `persistent`, le backend vérifie ou crée la session
`trader-runtime`. Si acpx répond que la session est absente, le backend lance
`acpx sessions new --name trader-runtime`, puis réessaie le prompt.

Reset de session recommandé :

- changement du contrat de prompt ou du schéma `Decision`;
- nouveau jour de marché, si on veut repartir d'une mémoire propre;
- plusieurs erreurs JSON consécutives;
- changement important de mandat;
- commande opérateur explicite.

Le reset peut être une commande CLI ultérieure, par exemple
`casys-trader session reset`, mais il n'est pas nécessaire pour la première
implémentation.

## Observabilité

Chaque décision doit pouvoir indiquer :

- `llm_provider`;
- `llm_model`;
- `llm_fallback_reason`;
- `llm_session_mode`;
- `llm_session_name`.

Si acpx expose un identifiant de turn stable dans le futur, il pourra être ajouté
comme `llm_session_turn_id`. La première version ne dépend pas de cette donnée.

Les champs doivent apparaître dans `state/current_report.json`,
`state/last_report.json` et `state/model_performance.jsonl` quand un fill est
créé.

## Error Handling

Le fail-safe ne change pas : toute erreur de transport, timeout, session acpx
introuvable non récupérable, sortie non JSON ou schéma invalide produit un HOLD.

Fallback :

- si le backend session échoue sur une erreur retryable, le routeur peut tomber
  sur le backend actuel `acpx exec` ou sur le backend OpenAI-compatible existant;
- le `fallback_reason` doit indiquer la cause, par exemple
  `spark-session:timeout` ou `spark-session:session_unavailable`.

Le mode persistent ne doit pas être utilisé en backtest au départ. Le backtest
reste stateless pour éviter la contamination entre pas historiques.

## Tests

Tests unitaires sans vrai acpx :

- `build_acpx_command` conserve `exec` en mode stateless.
- Le nouveau constructeur session génère `prompt -s trader-runtime`.
- Le router attache `llm_session_mode` et `llm_session_name` aux completions.
- Une erreur session retryable passe au backend suivant.
- `codex_client.decide` propage les métadonnées session dans `Decision`.
- Le daemon écrit ces métadonnées dans `model_performance.jsonl` sur fill.

Tests d'intégration ciblés :

- en mode persistent, un contexte stale ne réveille toujours pas le modèle;
- en backtest, le backend stateless reste utilisé même si l'env demande persistent.

## Scope initial

Inclus :

- mode opt-in par variables d'environnement;
- session globale `trader-runtime`;
- aucune permission outil côté modèle;
- metadata session dans les reports;
- fallback vers les backends existants.

Exclu de la première passe :

- sessions par symbole;
- CLI de reset avancée;
- lecture détaillée de l'historique acpx;
- dépendance à un turn id acpx stable.

