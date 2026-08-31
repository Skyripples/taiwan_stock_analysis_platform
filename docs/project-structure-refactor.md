# V3.15 Phase 0 Project Structure Refactor

## Directory policy

- Production entry points remain directly under `scripts/`.
- Research datasets, ablation, walk-forward validation, robustness checks, and pilots live under `scripts/research/`.
- One-time repair, cleanup, export, storage audit, and benchmark tools live under `scripts/maintenance/`.
- Browser-facing JavaScript tests live under `tests/frontend/`.
- Reports consumed or refreshed by current flows live under `data/analysis/current/`; reproducibility-only results live under `data/analysis/archive/`.
- Production migrations retain their original names and locations under `scripts/database/migrations/`.

Research and maintenance scripts may still be executed by file path. Their local compatibility loader adds the parent `scripts/` directory without changing Production imports.

## Shared validation code

`scripts/research/common/` owns the leakage-safe expanding-window boundary generator and the common binary metric contract. The canonical walk-forward validation now uses these helpers, including finite-value checks and `zero_division=0` for precision, recall, and F1.

## Deferred high-risk consolidation

### Global history updaters

`update_asia_market_history.py`, `update_semiconductor_history.py`, `update_vix_daily_history.py`, and `update_kospi_daily_history.py` reuse private helpers from `update_global_macro_history.py`. They duplicate CSV read/augment/atomic-write orchestration, but they also encode different target-date and completeness rules. Combining them now could alter historical alignment or overwrite behavior.

Recommended follow-up: introduce a declarative `HistoricalSeriesSpec` and a shared atomic row augmenter, then prove byte-equivalent output for each existing updater before replacing any entry point.

### Yahoo Provider

Yahoo providers are already auto-discovered by `ProviderRegistry`, but instrument definitions remain Python subclasses in `yahoo_provider.py`. Replacing them with config-generated provider classes changes registry identity, validation metadata, and output filenames.

Recommended follow-up: add an instrument registry beside the existing classes, compare provider keys and normalized JSON for every instrument, and switch only after a compatibility test proves identical output. Phase 0 intentionally leaves Production Yahoo behavior unchanged.
