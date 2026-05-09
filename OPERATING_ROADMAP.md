# Roadmap

AniMeta Nexus should grow in layers: showcase first, runtime ergonomics second,
exports third, guided workflows later.

The roadmap follows the same presentation doctrine as the product surface:
each milestone should add a named capability that brings another metadata
failure mode under control.

## Phase 1: Product Showcase

Goal: make the repository immediately understandable as a serious metadata
product.

Deliverables:

- polished README;
- static `docs/index.html`;
- deterministic demo corpus;
- generated command-center report;
- hero and workflow visuals;
- secret-safe public/private boundary.

## Phase 2: Runtime CLI

Goal: make the operational workflow easier to run and explain.

Possible command shape:

```powershell
python -m animeta_nexus.metadata_reconstruction_core --series-id 418367 --target-lang eng
python -m animeta_nexus.metadata_reconstruction_core --season-id 123456 --target-lang spa
python -m animeta_nexus.tvdb_contribution_rail --checkpoint-file animeta_nexus/metadata_reconstruction_ledger.json --target-lang eng
```

Planned improvements:

- clearer command grouping around named capability domains;
- dry-run mode;
- explicit review mode;
- better progress output;
- stricter target-language configuration.

## Phase 3: Export Layer

Goal: make the Distribution Rail useful even without contributing records back
to TVDB.

First exports:

- generic JSON;
- static HTML review;
- CSV inspection report.

Later exports:

- NFO-style sidecars;
- grouped series/season output;
- status-aware export bundles.

## Phase 4: Review Surface

Goal: give the Review Governance layer a dedicated inspection surface before
export or contribution.

Simplest version:

- static HTML generated from checkpoint/report;
- filters by status and issue type;
- before/after cards;
- source text and generated text;
- accept/reject markers.

Later:

- local editing UI;
- guided review workflow;
- selected-record export.

## Phase 5: Target-Language Policies

Goal: keep the Localization Policy Engine configurable without pretending every
language behaves the same.

Needed:

- target language code;
- human-readable language name;
- optional language-specific policy text;
- TVDB language validation;
- checkpoint grouping by target language.

## Phase 6: Guided Local Mode

Goal: make the workflow usable for maintainers who do not want to operate raw
scripts.

Possible forms:

- TUI wizard;
- local web UI;
- Windows wrapper;
- single-command guided flow.

Reality check: real runs still require API keys, provider credentials, and
possibly authenticated browser state. Those remain local-only.
