# Gérer le stockage des agents et du World Model

La rétention sépare deux autorités : les données générées par les agents et le
journal append-only du World Model. Les configurations sous `ops/` restent
réutilisables et ne sont jamais archivées par inférence.

## Sessions et données générées des agents

Toujours commencer par le plan sans écriture :

```bash
make storage-report DAYS=7
```

Puis appliquer exactement la même politique :

```bash
make storage-archive DAYS=7
```

Pour limiter une opération :

```bash
make storage-report DAYS=7 AGENT_HOMES="grok-home grok-home-medium" ONLY=sessions
make storage-archive DAYS=7 AGENT_HOMES="grok-home grok-home-medium" ONLY=sessions
```

Le registre de rétention est fail-closed :

| Source | Unité de cycle de vie | Politique actuelle |
|---|---|---|
| ACPX | record JSON fermé + stream NDJSON correspondant | archive `tar.zst` vérifiée |
| Grok low/medium | répertoire complet de session | archive si ancien et absent du registre actif |
| Codex | rollout référencé par `state_5.sqlite` | inventorié et protégé |
| Kimi | session provider | inventoriée et protégée tant que le contrat est inconnu |
| home inconnu | home entier | signalé `protected_unknown`, jamais supprimé |

Avant une suppression, l'usage LLM est extrait dans
`state/archive/llm_usage/`. Les racines déclarées sont confinées par `lstat`
(aucun suivi de symlink) sous `ops/`, sauf ACPX (`~/.acpx/sessions`) qui est
la seule racine externe explicite. Une session Grok n'est isolée que sous le
verrou exclusif `active_sessions.lock` ; une session ACPX n'est isolée que si
son `.stream.lock` peut être créé, ou si un lock O_EXCL existant enregistre un
pid prouvé mort (crash : le fichier restant ne bloque pas la rétention). Un
propriétaire vivant ou un payload opaque reste fail-closed. L'isolation est un rename atomique dans
`.retention-quarantine/` du root gardé, **après** un intent typé fsyncé
(`.retention-quarantine/.intent/<transaction_id>.json`, schéma
`agent_storage_quarantine_intent.v1`). Un `--apply` rejoue d'abord, sous le
lock global, toute transaction restante : restore si l'archive vérifiée et le
journal de réconciliation ne prouvent pas l'autorité de suppression ; finalize
seulement les sources manquantes quand ces deux preuves existent ; fail closed
sur intent corrompu, archive publiée absente/tronquée/remplacée, ou conflit
live+quarantaine. Tant que l'intent vérifié n'est pas fsyncé, SIGTERM/BaseException
restaure les sources live et abandonne l'intent. Une fois l'intent vérifié durable
(chemin canonique sous la racine d'archive + sha256, après `zstd`/tar/manifeste),
le crash laisse quarantaine et intent intacts : le `--apply` suivant restaure si
le journal est absent, ou finalise si le journal et l'archive (re-hashée, relue,
confinée no-follow) prouvent l'autorité. On ne restaure jamais dans la fenêtre
ambiguë journal-replace/fsync. Restore et replay sont strictement no-follow :
chaque ancêtre existant est validé, un parent symlinké/manquant/conflictuel
échoue fermé, le fingerprint est vérifié avant et après rename. La suppression
finale ouvre chaque répertoire `O_DIRECTORY|O_NOFOLLOW`, énumère depuis le fd
tenu, et unlink/rmdir via `dir_fd` : un swap directory→symlink entre lstat et
traversée n'énumère pas et n'unlink pas la cible. La première publication
d'archive agents refuse aussi une racine d'archive symlinkée, comme le replay. Une session
restaurée ou toujours live n'est jamais réconciliée. L'archive publiée est
fsyncée (fichier et répertoire), relue (`zstd -t`, tar décompressé, manifeste
et membres) puis seulement alors la quarantaine est détruite. Un
`--apply` prend un flock process-wide non-bloquant
(`state/retention/apply.lock`, `O_NOFOLLOW`) avant toute extraction LLM,
archive, purge ou réconciliation ; un autre apply propriétaire échoue
`blocked` / code 1. Un dry-run ne mute rien et peut seulement rapporter l'état
du lock. Avant la suppression irréversible, un journal fsyncé
`state/retention/pending-reconciliation.json` (schéma
`agent_storage_pending_reconciliation.v1`) mémorise source/provider et
identités. Il n'est compacté qu'après succès de la réconciliation ACPX/Grok ;
un crash se rejoue au `--apply` suivant même sans candidat d'archive, sans
retoucher aux sessions live. Un SQLite symlinké n'est jamais en
`DELETE`/`VACUUM` ; un `VACUUM` raté après `DELETE` est un
`partial_failure`. La réconciliation Grok ouvre la chaîne `home/sessions`
sans suivre de lien (surtout `sessions`), tient ces fds pendant
DELETE/journal/VACUUM, et se lie au sqlite via `/dev/fd/<n>` ; si la primitive
n'est pas disponible, elle échoue fermé. Les archives vivent dans `~/.acpx/archive/` avec un
répertoire `0700` et des fichiers `0600`. Le LaunchAgent impose `Umask 077`,
un `PATH` d'outils enfants (répertoire d'`uv`/ACPX/node plus chemins système),
sécurise `state/logs/` (`0700`/`0600`) et rotate un journal pretty-JSON
héritage avant de prétendre du JSONL. La réconciliation ACPX reconstruit
aussi ce PATH autour du binaire ACPX : le shebang `env node` ne dépend pas
du PATH interactif.

Le LaunchAgent installé (`make storage-schedule`) déclenche **uniquement** la
rétention des données générées des agents (`scripts.archive_agent_storage`).
L'export Parquet World Prediction (`make world-storage-export`) reste un
miroir shadow **manuel** : il n'est pas branché sur ce calendrier tant qu'une
décision de scheduling séparée n'est pas prise. Pas d'automatisation surprise.

Les historiques de prompts Grok sont des projections redondantes. Après une
archive réussie, la réécriture de `prompt_history.jsonl` se fait sous le
verrou provider ; un append concurrent fait échouer le remplacement (fail
closed) sans perdre la ligne nouvelle. L'index FTS est reconstruit depuis
`session_docs`, vérifié et compacté. Un `VACUUM` est précédé d'un journal
sidecar fsyncé (`session_search.sqlite.vacuum-journal`) : une course
interrompue rejoue DELETE+VACUUM au `--apply` suivant. Après un lot ACPX, l'index dérivé est reconstruit par ACPX lui-même
et comparé aux records survivants. Les auth, identités, presets, plugins,
skills, locks, WAL/SHM,
`ops/model-presets/` et `ops/launchd/` ne font pas partie des candidats.

## Prédictions World Model en Parquet

Planifier les journées UTC closes :

```bash
make world-storage-report
```

Publier les partitions vérifiées :

```bash
make world-storage-export
```

`BEFORE=YYYY-MM-DD` fixe une borne UTC exclusive. Une date après le jour UTC
courant (journée encore ouverte ou future) est refusée. Les fichiers sont publiés
sous :

```text
state/world_model_archive/
  world_shadow_predictions/
    schema=world-shadow-predictions.parquet.v1/
      recorded_date=YYYY-MM-DD/
        part-00000.parquet
        manifest.json
```

L'opération est idempotente et relit le Parquet pour comparer cardinalité,
identités, bornes, schéma, types physiques et hash ordonné au SQLite source.
Une cible d'archive symlinkée ou non régulière est refusée (aucun suivi de
lien, `O_NOFOLLOW` là où c'est portable). Le fichier produit et son
répertoire parent sont fsyncés avant d'être considérés vérifiés.
Une partition dérivée invalide (Parquet tronqué, manifeste manquant, coupure
secteur) est déplacée atomiquement sous `quarantine/` avec un motif forensique ;
le jour est reconstruit depuis `world_model.db` et les jours suivants continuent.
Rien n'est écrasé ni supprimé en silence. Elle ne modifie pas
`state/world_model.db` et ne libère donc pas encore ses anciennes lignes. Une
date déjà inscrite au registre froid (`world_prediction_cold_partitions`) n'est
jamais quarantenée, reconstruite ni remplacée par cet export : les lignes hot
tardives restent hot et sont seulement comptées. Une partition canonique
absente ou corrompue échoue fermé, sans reconstruction automatique.

Le cutover hors ligne est un opérateur distinct, dry-run par défaut, et refuse
un `daemon.pid` vivant. Il s'applique à une copie de `state/` (ou à l'état
source seulement après arrêt et lease exclusif) :

```bash
uv run python -m scripts.cutover_world_predictions --state-dir state --before YYYY-MM-DD
uv run python -m scripts.cutover_world_predictions --state-dir state --before YYYY-MM-DD --apply
uv run python -m scripts.cutover_world_predictions --state-dir state --before YYYY-MM-DD --apply --adopt
uv run python -m scripts.cutover_world_predictions --state-dir state --resume
```

`--apply` sans `--adopt` construit un candidat compact à côté de la source, en
conserve une sauvegarde SQLite vérifiée, et laisse `world_model.db` intact.
`--adopt` exige `--apply` (ou `--resume`) : checkpoint WAL complet des deux
fichiers, `journal_mode=delete` vérifié (un checkpoint incomplet ou occupé
bloque l'opération), puis remplacement atomique. Les sidecars WAL/SHM
actifs ne sont jamais supprimés manuellement. Un journal `world_model.db.cutover.json` rend
l'opération reprenable ; `--resume` est obligatoire s'il reste. Toute
présence de ce journal bloque l'export et les accès partagés, y compris un
statut `success` ou `rolled_back` ; seul l'opérateur archive ou retire le
fichier après finalisation durable.

La CLI ne propose pas de restauration automatique. La sauvegarde permet une
restauration hors ligne avant toute nouvelle écriture. Après de nouvelles
écritures, il faut réhydrater les partitions froides dans une copie de la base
courante : restaurer l'ancienne sauvegarde ferait perdre ces écritures.

Le dry-run / preview est en lecture (lease partagé `create=False`). Il ne crée
pas d'archive, de journal, de backup ni de candidat. Une connexion SQLite
`mode=ro` peut créer des sidecars `-wal`/`-shm` s'ils étaient absents ;
les données source restent inchangées.

Journal, étapes et reprise : `acquired` → `snapshot` → `archives_verified` →
`candidate` → `compacted` → `verified` → `prepared` (sans adopt) ou
`pre_swap` → `swapped` → `success`. Un marqueur `pre_swap` seul ne dispense
pas des preuves : avant chaque remplacement on revalide la source logique
contre la sauvegarde figée (toutes les tables métier), le candidat, et les
empreintes **après checkpoint** (distinctes de l'empreinte physique initiale ;
un checkpoint source peut changer les octets sans changer les données). Une
mutation WAL-only ou un candidat substitué refuse la reprise ; on ne restaure
pas la sauvegarde. Après swap, un marqueur de génération ne suffit pas :
intégrité, catalogue froid/archives canoniques et parité attendue avant
d'effacer le journal. Le LaunchAgent de rétention agents n'exporte pas et ne
compacte pas le World Model.
