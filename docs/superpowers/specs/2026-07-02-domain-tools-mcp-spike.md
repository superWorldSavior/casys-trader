# Domain tools via serveur MCP daemon-owned — spec de spike

**Date** : 2026-07-02
**Status** : spec approuvée pour spike (décision Erwan) ; à exécuter AVANT les outils d'action
**Amont** : `docs/reference/agent-tools.md` (couche d'outils livrée ; la spec
historique 2026-06-29 reportait MCP), `docs/superpowers/specs/2026-07-02-acpx-fork-upstream-inventory.md` (#366
schema/tools upstream), état actuel : protocole-maison JSON-en-prose livré et actif.

---

## 1. Intention

Remplacer le protocole-maison (catalogue en prose dans le prompt + parsing JSON
tolérant côté daemon + boucle deux-passes hand-rolled) par le **mécanisme
d'outils natif de codex, qui est MCP** — tout en gardant la philosophie qu'Erwan
valide : « on appelle un agent ET on lui tend les outils au moment de l'appel »,
sans serveur MCP externe à administrer.

Le spike valide la faisabilité et l'ergonomie AVANT d'y coder les outils d'action
(pour ne pas développer deux fois : action tools directement en MCP si le spike
tient).

## 2. Décisions déjà prises (cadre, pas à re-débattre)

- **Flavor B — le daemon INCARNE le serveur MCP** (pas un sous-process séparé,
  pas un service à gérer). À `parallelism=1` il n'y a qu'un contexte de décision
  actif → les handlers lisent le `ToolContext` du cycle courant en mémoire, zéro
  plomberie de contexte (c'est ce qui rend B gratuit vs flavor A).
- **Pas de package `casys-mcp-server`** (YAGNI) : module in-daemon à frontière
  propre, extractible en lib SEULEMENT si un 2e consommateur apparaît (outillage
  opérateur / recherche offline / codex perso d'Erwan).
- **Isolation codex** : le codex du trading tourne avec sa PROPRE config
  (`CODEX_HOME` dédié), ZÉRO serveur MCP hérité (pas de serena) — séparer le
  codex du trading du codex quotidien d'Erwan.
- **Outils lecture-seule** exposés en MCP ; le daemon reste **seul exécuteur de
  l'ORDRE final** (la décision finale reste le contrat JSON gaté :
  execution/exit-plan/RiskGate inchangés). Donner l'accès outil live au brain
  n'est pas un problème (Erwan : « on le laisse totalement décider »).

## 3. Non-goals

- Quitter acpx/Codex pour l'accès API direct (PydanticAI / SDK natif). C'est une
  décision de FONDATION séparée, économique autant que technique (pay-per-token
  vs abonnement Codex) — à trancher sur un chiffrage dédié, hors de ce spike.
- Exposer les outils à un 2e consommateur (opérateur, offline). Plus tard.
- Migrer les outils d'action dans ce spike (ils viennent APRÈS, une fois le
  transport MCP validé).

## 4. Inconnues à lever (le cœur du spike)

1. **codex-acp accepte-t-il qu'on lui impose UN serveur MCP unique** (le nôtre)
   et **zéro autre** (pas de serena) via une config codex dédiée ? C'est LE test.
2. Transport MCP retenu : le daemon expose un serveur **stdio** (codex le spawn
   et parle par pipes) ou **HTTP/SSE local / unix socket** (le daemon écoute,
   codex s'y connecte) ? Le stdio est le plus standard pour codex ; le socket
   colle mieux à « le daemon long-vivant incarne le serveur ». À trancher par
   essai.
3. Comment le serveur MCP accède au `ToolContext` du cycle : lecture mémoire
   directe (si daemon = serveur, même process) vs canal IPC (si stdio subprocess).
   Flavor B vise la lecture directe.
4. Ergonomie : le tool-calling natif de codex est-il plus fiable/plus lisible que
   notre boucle-maison ? (moins de HOLD par parsing raté, traces plus propres).
5. Coexistence pendant la migration : peut-on garder le protocole-maison actif
   (flag) le temps de comparer, sur un sous-ensemble d'outils ?

## 5. Périmètre du spike (minimal, jetable)

- Exposer **2 outils lecture-seule** en MCP : `get_freshness` (simple, facts déjà
  calculés) et `recall_learnings` (le plus à valeur, contexte non-trivial).
- Config codex isolée : `CODEX_HOME` dédié, MCP servers = { notre serveur }, rien
  d'autre. Vérifier `ps` : aucun serena spawné pour le codex trading.
- Un cycle de décision de bout en bout où le brain APPELLE ces 2 outils en MCP
  natif, le daemon sert la réponse depuis le `ToolContext` du cycle, la décision
  finale reste le contrat JSON gaté.
- Mesure : latence de l'appel outil, propreté des traces (l'usage doit toujours
  atterrir dans `runtime.tool_calls` — dériver depuis les événements MCP), et
  confirmation « zéro serena ».

## 6. Critères de réussite (go/no-go pour généraliser)

- Le codex trading tourne avec 0 serveur MCP hérité (0 serena), seulement le nôtre.
- Un tool-call MCP natif réussit et sa réponse influence la décision finale.
- Le `ToolContext` du cycle est servi correctement (bon symbole, bonnes bornes).
- La décision finale passe toujours par les gates (aucun chemin vers `broker.submit`
  hors execution/exit-plan/RiskGate).
- Traçabilité préservée (usage outil auditable par décision).
- Latence acceptable (< budget cycle ; l'embed OpenAI de recall reste le poste lourd).

## 7. Si le spike réussit — plan de généralisation

1. Porter les 6 autres outils lecture-seule en MCP.
2. Coder les outils d'ACTION (Phases 4-5 du design agent-tools) DIRECTEMENT en
   MCP (set_next_wake, propose_indicator_watch, cancel_watch, record_learning,
   puis propose_order) — jamais dans le protocole-maison.
3. Retirer le protocole-maison JSON (`_TOOL_CATALOG`, `parse_batch_or_tool_calls`,
   la tournée `_run_tool_round`) une fois la parité mesurée.
4. Garder le `TOOL_REGISTRY` et les `ToolContext`/handlers : SEULE la couche de
   livraison change (prose→MCP), pas les outils ni le contrat.

## 8. Si le spike échoue

Rester sur le protocole-maison (il marche, il est testé) et coder les outils
d'action dedans — en documentant pourquoi MCP n'a pas tenu (contrainte
codex-acp / config / contexte). Rouvrir si le fork acpx ou codex évolue.

## 9. Risques

- codex-acp pourrait ne pas permettre d'imposer une config MCP propre → l'inconnue
  #1 est bloquante ; à tester en TOUT PREMIER.
- Un serveur MCP daemon-owned ajoute une surface longue-vie au daemon (flavor B) ;
  garder le contrat étroit, lecture seule, borné.
- Ne PAS laisser le spike grossir : 2 outils, 1 cycle, go/no-go. Pas de migration
  dans le spike.
