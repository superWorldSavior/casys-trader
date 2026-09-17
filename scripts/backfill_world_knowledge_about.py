"""Backfill news/company ABOUT artifacts + relations into the World Model.

The command is read-only by default. Pass ``--apply`` to run one forward
sweep (see ``AboutWriterService``): ensure the extended ontology revision is
published, bridge live-window notes + verified briefs, append artifacts
(rerun-safe no-ops), link relations to store receipts, assert them to the
graph.

Point-in-time semantics: the ledger gates availability by record time, so
backfilled rows join at cutoffs >= their store receipt, never historically.
Only live-window notes are selected by default; ``--all`` forces a full
select (repro/debug).

Usage::

    uv run python scripts/backfill_world_knowledge_about.py
    uv run python scripts/backfill_world_knowledge_about.py --apply
    uv run python scripts/backfill_world_knowledge_about.py --apply --all
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trader.application.world_model.about_bridge import (  # noqa: E402
    build_company_about,
    build_news_about,
)
from trader.application.world_model.about_writer import (  # noqa: E402
    AboutWriterService,
    select_live_notes,
)
from trader.application.world_model.issuer_registry import (  # noqa: E402
    build_issuer_registry,
)
from trader.application.world_model.ontology_bootstrap import (  # noqa: E402
    WorldOntologyAttestation,
    derive_extended_ontology,
)
from trader.application.world_model.world_scope_resolver import (  # noqa: E402
    WorldScopeResolver,
)
from trader.infrastructure.files.company_briefs import load_company_briefs  # noqa: E402
from trader.infrastructure.files.family_catalog_config import (  # noqa: E402
    load_family_catalog,
)
from trader.infrastructure.state_db.situation_memory_store import (  # noqa: E402
    ensure_notes_schema,
    read_situation_notes,
)
from trader.infrastructure.state_db.world_graph_store import (  # noqa: E402
    WorldGraphStore,
)
from trader.infrastructure.state_db.world_knowledge_store import (  # noqa: E402
    WorldKnowledgeStore,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="state", help="State root (default: state)")
    parser.add_argument("--config-dir", default="config", help="Config root (default: config)")
    parser.add_argument(
        "--briefs-root",
        default=None,
        help="Company briefs root (default: <state>/company_intelligence)",
    )
    parser.add_argument("--apply", action="store_true", help="Write. Default is dry-run.")
    parser.add_argument(
        "--all",
        dest="select_all",
        action="store_true",
        help="Select every note, including expired windows (repro/debug).",
    )
    return parser.parse_args(argv)


def _report_counts(title: str, counts: dict[str, int]) -> None:
    rendered = ", ".join(f"{key}={counts[key]}" for key in sorted(counts)) or "none"
    print(f"{title}: {rendered}")


def main() -> int:
    args = _parse_args()
    state_dir = Path(args.state_dir)
    config_dir = Path(args.config_dir)
    briefs_root = Path(args.briefs_root) if args.briefs_root else state_dir / "company_intelligence"
    knowledge_root = state_dir / "world_knowledge"
    graph_db = state_dir / "world_model.db"
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[backfill] mode={mode} state={state_dir} knowledge={knowledge_root}")
    started = time.monotonic()

    mapping = WorldScopeResolver.load(config_dir).mapping
    catalog = load_family_catalog(config_dir)
    corpus_briefs = load_company_briefs(briefs_root)
    print(
        f"[backfill] mapping={mapping.mapping_id} symbols={len({e.anchor.instrument for e in mapping.entries})}"
        f" families={len(catalog.families)} briefs={len(corpus_briefs.briefs)}"
        f" skipped_brief_files={corpus_briefs.skipped_count}"
    )
    registry = build_issuer_registry(corpus_briefs.briefs, mapping).registry
    print(f"[backfill] issuer entries={len(registry.entries)}")

    notes_db = state_dir / "situation_memory.db"
    if args.apply:
        ensure_notes_schema(notes_db)
        notes = read_situation_notes(notes_db)
        notes_source = "read"
    else:
        try:
            notes = read_situation_notes(notes_db)
        except FileNotFoundError:
            notes = []
            notes_source = "missing"
        else:
            notes_source = "read"
    selected = notes if args.select_all else select_live_notes(notes)
    print(
        f"[backfill] notes={len(notes)} selected={len(selected)}"
        f" expired_skipped={len(notes) - len(selected)}"
        f" notes_source={notes_source}"
    )

    if not args.apply:
        _, _, expected = derive_extended_ontology(mapping, registry, catalog)
        news_build = build_news_about(selected, mapping, ontology_revision=expected.revision_id)
        company_build = build_company_about(
            registry, corpus_briefs.briefs, catalog, ontology_revision=expected.revision_id
        )
        print(f"[backfill] extended revision={expected.revision_id}")
        print(
            f"[backfill] news drafts={len(news_build.drafts)}"
            f" relations={sum(len(d.relations) for d in news_build.drafts)}"
        )
        _report_counts("[backfill] news excluded", dict(news_build.exclusion_counts))
        print(
            f"[backfill] company drafts={len(company_build.drafts)}"
            f" relations={sum(len(d.relations) for d in company_build.drafts)}"
        )
        _report_counts("[backfill] company excluded", dict(company_build.exclusion_counts))
        elapsed = time.monotonic() - started
        print(f"[backfill] dry-run done in {elapsed:.1f}s; no writes. Pass --apply to write.")
        return 0

    graph_store = WorldGraphStore(graph_db)
    try:
        attestation = WorldOntologyAttestation(
            graph_store, mapping, issuer_registry=registry, family_catalog=catalog
        )
        writer = AboutWriterService(
            graph=graph_store,
            knowledge=WorldKnowledgeStore(knowledge_root),
            ontology=attestation,
        )
        news = writer.write_news(notes, mapping=mapping, select_live=not args.select_all)
        company = writer.write_company(corpus_briefs.briefs, registry=registry, catalog=catalog)
    finally:
        graph_store.close()

    elapsed = time.monotonic() - started
    print(f"[backfill] news revision={news.revision_id}")
    print(f"[backfill] news drafts={news.drafts} relations={news.relations_linked}")
    _report_counts("[backfill] news excluded", dict(news.excluded))
    print(f"[backfill] company drafts={company.drafts} relations={company.relations_linked}")
    _report_counts("[backfill] company excluded", dict(company.excluded))
    print(
        f"[backfill] done in {elapsed:.1f}s:"
        f" artifacts appended={news.artifacts_appended + company.artifacts_appended}"
        f" reused={news.artifacts_reused + company.artifacts_reused}"
        f" relations linked={news.relations_linked + company.relations_linked}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
