# Architecture — casys-trader

> **Type** : Explanation (Diataxis).
> **État consolidé** : 2026-08-20. Le code et les pages Reference priment sur
> les plans historiques.

Cette façade maintient les anciens titres et ancres §1–14. L'explication est
désormais découpée par sujet dans [docs/explanation/](explanation/README.md).

## 1. Vue d'ensemble

Voir [vue d'ensemble et carte modulaire](explanation/architecture/overview.md).

### 1.1 Carte modulaire actuelle

Voir les [frontières et modules structurants](explanation/architecture/overview.md).

### 1.2 Niveaux d'architecture

Voir les [frontières](explanation/architecture/overview.md).

## 2. Diagramme ASCII — flux d'un cycle

Voir le [flux de cycle](explanation/architecture/decision-cycle.md).

## 3. Le cycle de décision — de bout en bout

Voir le [cycle de décision](explanation/architecture/decision-cycle.md).

### 3.1 Sélection des symboles dus
### 3.2 Chargement des barres & fraîcheur
### 3.3 Construction du contexte partagé
### 3.4 Gate de pertinence (D7 étage A)
### 3.5 Plans armés — exécution sans LLM (D7 étage B)
### 3.6 Dispatch LLM — queue grain-symbole et compatibilité batch
#### Ce que reçoit le brain pendant une décision d'entrée
### 3.7 Validation & gates pré-exécution
### 3.8 Exécution et persistance

Les étapes §3.1–3.8 sont expliquées dans le [cycle de décision](explanation/architecture/decision-cycle.md).

## 4. Les chemins de sortie

Voir [exécution, sorties et état](explanation/architecture/execution-state.md).

### 4.1 Sortie automatique — exit_engine
### 4.2 Sortie discrétionnaire LLM
### 4.3 Sortie via plan armé / resolve (D11)

## 5. Stops & résolution — `resolve_exit_plan`

Voir les [chemins de sortie](explanation/architecture/execution-state.md).

## 6. Univers & rotation (D9 / D10)

Voir [intelligence et apprentissage](explanation/architecture/intelligence-learning.md).

### 6.1 Radar (Tier 1 — daily, 0 LLM)
### 6.2 Rotation & hot-sets par venue (D10)

## 7. Veille / réveils — indicator_watch

Voir [veilles et réveils](explanation/architecture/execution-state.md).

## 8. État persistant — `state/`

Voir [état persistant](explanation/architecture/execution-state.md).

## 9. Intégration LLM / acpx

Voir [LLM, transport et outils](explanation/architecture/operations-governance.md).

### 9.1 Routage — `trader/agent/llm.py`
### 9.2 Sessions ACP et isolation — `infrastructure/llm/acpx_backend.py`
### 9.3 `trader/agent/client.py` — façade transport, protocole séparé

## 10. Outils domaine — la tournée d'outils du LLM

Voir [LLM, transport et outils](explanation/architecture/operations-governance.md).

## 10.1 Logging et dépendances du refactor

Voir [logs, preuves et pilote de processus](explanation/architecture/operations-governance.md).

## 11. Mémoire outcome-weighted — recall des learnings

Voir [trois mémoires, trois rôles](explanation/architecture/intelligence-learning.md).

## 12. Gestion des données — rotation et archives

Voir [état persistant](explanation/architecture/execution-state.md).

## 13. Pipeline d'intelligence global → régional → symbole

Voir [Univers : sélectionner l'attention](explanation/architecture/intelligence-learning.md).

## 14. Pilote de processus : observer sans décider

Voir [logs, preuves et pilote de processus](explanation/architecture/operations-governance.md).
