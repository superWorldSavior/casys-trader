# Consolidation du mandat Univers → Trader — design

- **Date** : 2026-07-11
- **Statut** : à implémenter (7 lots, 2 vagues)
- **Auteurs** : Erwan + Claude (fact-check Codex)
- **Portée** : chaîne agentique `GlobalSituationDigest → GlobalFamilyBoard →
  GlobalUniversePosture → RegionalUniverseMandate → Trader`.
- **Prédécesseur** : `2026-07-09-universe-intelligence-pass-design.md` (§5 = contrat
  de mandat cible, resté sur le papier). Ce document le rend exécutable et corrige
  la migration à moitié faite.

> Principe directeur : **le LLM digère l'information non structurée et formule des
> vues/mandats ; le code déterministe transforme l'intention en chiffres.** Univers
> émet une posture + une enveloppe d'exposition bornée, jamais un ordre, jamais une
> quantité. Le trader garde 100 % de la main sur plans, timing, tailles, stops.

---

## 1. Diagnostic — faits vérifiés (fichier:ligne)

Tous les points ci-dessous sont confirmés dans le code ou les données live au
2026-07-11.

| # | Défaut | Preuve | Impact |
|---|--------|--------|--------|
| D1 | **Garde V2 rejette le mandat** | `agent.py:111` refuse `contract_version != "universe.v1"` ; `prompt.py:34` exige `symbol_mandates` ; `prompt.py:87` classe cette réponse en `universe.v2`. Les trois lignes viennent du **même commit** `b42f4b0`. | Un modèle qui obéit au prompt est **toujours rejeté** → 0 mandat écrit sur 441 décisions. |
| D2 | **Config micro morte** | `config/company_intelligence.yaml:17-19` déclare `summary_chars_per_symbol: 240` + `max_points_per_symbol: 5`. Le code hardcode `summary[:600]` + `[:2]` + `[:8]` (`domain/universe/intelligence.py:119-124`). La section `projection:` n'est jamais lue. | Payload micro ~2,5× trop gros (~103 Ko EU pour 40 candidats). |
| D3 | **Dict-stringify du thesis.summary** | `domain/company/intelligence.py:457` : `summary=_clean_text(payload.get("summary"))` ; `_clean_text` fait `str(value)` (`:75-78`). Le LLM renvoie `summary` comme objet `{point,...}` → `str(dict)` = `"{'point': ...}"`. **574/697 briefs history (82 %), 116/127 current (91 %)**. Cause racine : le prompt `company_micro/prompt.py:51-57` ne définit jamais le schéma de `company_thesis`. | Bruit `{'point': ...}` injecté brut au trader ET à l'agent univers. |
| D4 | **`pillars` toujours vide → `drivers == catalysts`** | `domain/universe/intelligence.py:107-109` : `drivers` part de `pillars[:2]` (0/697 briefs ont des pillars) puis se complète avec `catalysts[:2]` = identique à `catalysts` (`:121`). | Duplication systématique dans la projection. |
| D5 | **`family_postures` orpheline** | Produite (`prompt.py:66`), écrite au run record (`universe_intelligence_runtime.py:310`), **jamais** passée à `_build_prepared_mandate` (`:726-766`), absente de `UniverseMandate`/`SymbolMandate`. | La posture famille décidée par l'agent n'atteint jamais le trader. |
| D6 | **`family_context` = snapshot quanti, pas la posture décidée** | `_build_prepared_mandate` remplit `family_context = request.family_snapshot.get(family, {})` (counts/bias/regime), pas la posture narrative de l'agent. | Le trader reçoit des chiffres d'entrée, pas le jugement. |
| D7 | **`portfolio_context` / `portfolio_posture` absents** | Aucune occurrence dans `trader/**/*.py`. Contrat spec §5 non implémenté. | Aucune enveloppe d'exposition transmise. |
| D8 | **Double injection micro** | `company_context` injecté 2× : sous `company_intelligence` (`planner_batch.py:150-151`) ET sous `universe_mandate.symbol_mandate.company_context` (propagé par `_build_prepared_mandate`). Même source `CompanyIntelligenceStore`. | Recopie redondante au trader. |
| D9 | **Board live jamais prouvé** | `state/global_family_boards/current.json` : `status: unavailable`, `scope_venues: []`. | Le GFB n'a pas produit un cycle complet. |
| D10 | **Pas de brief macro global** | `news_macro_runtime.py` produit 3 briefs par venue ; le « global » n'existe que via les résumés famille du GFB. | Aucune narration macro globale (Fed/taux/USD) partagée. |
| D11 | **Univers mono-tour** | `LlmRouter.complete()` (`domain/llm.py:41`) sans tools ; acpx `--allowed-tools ""` (`acpx_backend.py:186-189`) ; `retrieval_status` rejeté ≠ `not_enabled` (`composition.py:150-151`). | Pas de pull possible ; tout est poussé statiquement. |

**Ce qui existe déjà et sera réutilisé** : le write path complet du mandat
(`_build_prepared_mandate → write_prepared → activate → active_slice_for_symbol`,
`universe_mandate_store.py`), le `SymbolMandate`/`UniverseMandate`
(`domain/universe/mandate.py`), le `GlobalFamilyBoard`
(`domain/universe/global_family_board.py`), les projections situation/company
(`domain/universe/intelligence.py`).

---

## 2. Architecture cible — 5 artefacts, séparation faits / décision

```
GlobalSituationDigest   (FAITS macro globaux, 1×)          ── nouveau (L5)
        │
        ▼
GlobalFamilyBoard       (FAITS comparatifs cross-région)   ── existe
        │
        ▼
GlobalUniversePosture   (DÉCISION région/famille, 1×)      ── nouveau (L6)
        │
        ▼   (injecté identique dans les 3 passes régionales)
RegionalUniverseMandate (hotlist + mandat par symbole)     ── débloqué (L1) + enrichi (L3)
        │
        ▼
Trader                  (plans, timing, entrées/sorties)   ── allégé (L4)
        │
        ▼
Policy déterministe + RiskGate  (chiffres, tailles, stops)
```

**Invariant d'ordre** : personne ne lit la *décision* d'un autre pair. Le board
reste factuel, la posture globale est produite **une fois** et injectée
identique dans TW/EU/US. Cela supprime la dépendance « TW lit EU lit TW ».

**Séparation des rôles agentiques** :
- **Agent Univers** (2 niveaux, même rôle) : global (posture région/famille) puis
  régional (hotlist + mandat par symbole). Digère macro + micro + régime +
  portefeuille sticky. N'émet jamais qty/stop/sizing.
- **Agent Trader** (par symbole) : reçoit le mandat synthétique + sa position/plans
  + structure marché + capacité risk. Décide entrer/attendre/armer/gérer. **Peut
  contredire le mandat** si la structure locale ne confirme pas.

---

## 3. Contrat de mandat enrichi — « comment on donne le mandat au trader »

C'est le cœur. Chaque symbole du trader reçoit **un objet mandat** qui remplace la
double injection micro. Extension du `SymbolMandate` existant :

```jsonc
{
  "symbol": "AIR.PA",
  "mandate_ref": { "mandate_id": "...", "venue": "EU", "status": "active",
                   "valid_until": "2026-07-12T..." },
  "role": "core_candidate|hedge_candidate|watch_only|managed_existing",
  "posture": "constructive|watch_only|reduce_only",
  "allowed_sides": ["long"],            // permission advisory (jamais un gate dur)
  "directional_view": "long_bias|short_bias|two_sided|neutral",  // NOUVEAU : la vue
  "why_selected": "synthèse courte macro+micro+famille",
  "family_context": {                   // NOUVEAU sens : posture DÉCIDÉE (plus le snapshot)
     "family": "defense_aero_eu",
     "posture": "favored|selective|avoid",   // ← family_postures transmise ICI (D5)
     "note": "défense EU favorisée, déjà bien représentée en sticky"
  },
  "company_context": {                  // synthèse micro COURTE + refs (plus le brief brut)
     "summary": "...", "event_risk": "none_known|earnings_soon|...",
     "source_refs": ["news_brief:..."]
  },
  "portfolio_context": {                // NOUVEAU (D7) : enveloppe advisory, jamais un ordre
     "exposure_note": "éviter d'augmenter le net long semis TW",
     "risk_notes": ["rebond possible après selloff multi-jours"]
  },
  "confidence": "fresh|partial|stale|unknown",
  "reduce_close_always_allowed": true
}
```

Au niveau **venue** (`UniverseMandate`), on ajoute :

```jsonc
{
  "portfolio_posture": { "gross_mode": "normal|cautious|risk_off",
                         "net_bias": "long|short|neutral", "notes": [...] },
  "family_postures": { "defense_aero_eu": "favored", "semis_tw": "selective" }
}
```

**Règles (déjà appliquées en partie, à préserver)** :
- `_symbol_mandates` (`prompt.py:160`) interdit `qty/stop/sizing/risk_pct` — garder.
- `role/posture/allowed_sides/directional_view` = mandat, jamais ordre.
- Chaque résumé court, daté, TTLé, sourcé par référence — jamais de flux news brut.
- La baseline déterministe reste visible pour mesurer l'alpha de l'override.

---

## 4. Flux micro — compact push + exact pull (tool loop)

**Décision Erwan (2026-07-11) : tool loop complet.** L'agent Univers passe de
mono-tour à multi-tours.

**Push (défaut)** : carte minuscule par candidat (posture micro, confiance,
fraîcheur, une phrase de thèse, un driver, un risque, `brief_ref`) — ~150–250 B/sym
au lieu de ~2,5 Ko.

**Pull (à la demande)** : tool borné et typé
```
get_company_briefs(symbols: list[str], sections: list[str], max_chars: int)
  sections ⊂ {thesis, catalysts, risks, financial_snapshot, earnings, business}
```
L'agent identifie 5–10 dossiers prometteurs/ambigus et tire les sections exactes.
Récupération **exacte par symbole** (pas de RAG vectoriel qui peut omettre
silencieusement un candidat). Le RAG `situation_memory.db` reste réservé aux
analogies historiques (hors périmètre).

**Trader** : reçoit le mandat synthétique (§3) + un **delta micro borné** seulement
si une news est plus fraîche que le mandat. Plus de recopie du brief complet (D8).

**Invariants du tool** : lecture seule, borné (`max_chars`), déterministe (mêmes
args → même sortie), pas d'accès fichier libre (le store reste canonique derrière le
tool). Fail-close si le brief est absent (`status: missing`, pas d'invention).

---

## 5. GlobalSituationDigest (L5)

Brief macro **global** compact produit 1× par cycle à partir des news globales +
séries macro déjà collectées (FOMC/CPI/DBnomics — cf. chantier macro/fondamental).
Contenu borné : régime risk-on/off, taux/USD/oil, 3–5 points datés+sourcés.
Référencé par : les 3 briefs régionaux, le GFB, la `GlobalUniversePosture`.
Comble D10. Ne remplace pas les briefs régionaux ; les surplombe.

---

## 6. GlobalUniversePosture (L6)

Produite **une fois** par le rôle Univers, à partir de `GlobalFamilyBoard +
GlobalSituationDigest + sticky` :

```jsonc
{
  "as_of": "...", "valid_until": "...",
  "venue_posture": { "TW": "favor", "EU": "selective", "US": "watch" },
  "family_priority": { "favored": ["defense_aero_eu"], "deprioritized": ["semis_tw"] },
  "gross_mode": "normal|cautious|risk_off", "net_bias": "long|neutral|short",
  "rationale": "..."
}
```

Injectée identique dans les 3 passes régionales (résout l'ordre-dépendance).
La passe régionale reste seule décideuse de sa hotlist ; la posture est un cadre,
pas un quota.

---

## 7. Plan d'implémentation — 7 lots, 2 vagues

Propriété : **Codex (Terra xhigh)** pour le code ; **Claude** pour tous les prompts
et définitions de tool. Fichiers disjoints par lot pour paralléliser sans collision
git ; les lots qui partagent un fichier sont séquencés.

### Vague 1 — socle (débloque + nettoie + enrichit)

| Lot | Objet | Fichiers | Owner | Dépend de |
|-----|-------|----------|-------|-----------|
| **L1** | Fix garde V2 : `not in {"universe.v1","universe.v2"}` (rejeter seulement `legacy.add_remove.v1`) | `agent.py:111` + test | Codex | — |
| **L2c** | Nettoyage micro : garde dict dans `from_mapping:457` (extraire `.point`), appliquer config `240/5`, dédup `drivers` | `domain/company/intelligence.py`, `domain/universe/intelligence.py` + tests | Codex | — |
| **L2p** | Prompt : définir le schéma `company_thesis` (`summary`=prose, `pillars`=[points]) | `agent/company_micro/prompt.py` | **Claude** | — |
| **L3** | Mandat enrichi : `portfolio_context`+`directional_view` (SymbolMandate), `portfolio_posture`+`family_postures` (UniverseMandate), `family_context`=posture décidée, brancher `decision.family_postures` | `domain/universe/mandate.py`, `runtime/universe_intelligence_runtime.py` (`_build_prepared_mandate`) + tests | Codex | L1 |
| **L4** | Dédup micro trader : mandat = source primaire, company_context = delta borné | `runtime/trader_research_context.py`, `planner_batch.py` | Codex | L3 |
| **L4p** | Prompt trader : documenter le mandat comme source primaire + delta micro | `agent/.../prompts.py` | **Claude** | L3 |
| **L5** | GlobalSituationDigest : domaine + runtime + collecte | nouveau `domain/situation/global_digest.py` + runtime | Codex | — |

### Vague 2 — deux niveaux + pull

| Lot | Objet | Fichiers | Owner | Dépend de |
|-----|-------|----------|-------|-----------|
| **L6** | GlobalUniversePosture : domaine + production 1× + injection régionale | nouveau module + `universe_intelligence_runtime.py` | Codex | L3, L5 |
| **L7c** | Tool loop : carte minuscule + `get_company_briefs` + activation tools acpx | `agent/universe/agent.py`, backend, nouveau tool | Codex | L1 |
| **L7p** | Prompt Univers multi-tours + définition du tool | `agent/universe/prompt.py` | **Claude** | L7c |

**Ordre de dispatch** :
1. Batch parallèle A (disjoints) : **L1 + L2c + L5** (Codex) ; **L2p** (Claude) en parallèle.
2. Après L1 vert : **L3** (Codex).
3. Après L3 vert : **L4 + L6** (Codex) ; **L4p** (Claude) ; **L7c** (Codex, dépend L1) ; **L7p** (Claude).

---

## 8. Invariants & tests (test-first)

- **INV-1** : une réponse univers valide avec `symbol_mandates` est acceptée
  (contract `universe.v2`) et écrit un mandat par symbole. *(garde D1)*
- **INV-2** : `company_thesis.summary` est toujours une chaîne de prose ; un payload
  LLM renvoyant un objet `{point,...}` est réduit à `.point`. *(garde D3)*
- **INV-3** : la projection micro respecte `summary_chars_per_symbol` et
  `max_points_per_symbol` de la config. *(garde D2)*
- **INV-4** : `drivers` et `catalysts` ne sont pas identiques quand `pillars` est
  non vide ; `drivers` vide plutôt que dupliqué quand `pillars` est vide. *(garde D4)*
- **INV-5** : `family_postures` décidée par l'agent apparaît dans le mandat par
  symbole (`family_context.posture`). *(garde D5/D6)*
- **INV-6** : le trader ne reçoit `company_context` qu'une fois (mandat), plus la
  double injection. *(garde D8)*
- **INV-7** : le mandat ne contient jamais `qty/stop/sizing/risk_pct`. *(préservé)*
- **INV-8** : `get_company_briefs` est déterministe, borné, lecture seule, fail-close
  sur brief absent. *(garde L7)*

---

## 9. Journalisation & mesure (sans bloquer la livraison)

On livre tout, on n'attend pas pour observer — mais on **instrumente** dès le
départ : chaque mandat porte `mandate_id`, `brief_refs`, `agent_run_id` et la
baseline déterministe reste visible, pour pouvoir mesurer l'alpha de l'override
*a posteriori* sans re-livraison. Jointure :
`news item → run scout → brief → run univers → mandat → décision → fill/outcome`.
