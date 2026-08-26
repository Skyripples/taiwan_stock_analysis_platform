# V3.13 Global Market Data Foundation

## Scope

This phase is an audit and architecture design only. It does not download bulk history, alter the production PostgreSQL schema, connect the candidate registry to daily updates, or change Prediction, Calibration, Market Score, stock analysis, or the frontend.

The research target is the direction/return of a Taiwan index or stock from the target trading-day open to close for T+1, T+3, and T+5 horizons. Every feature must be demonstrably available before **09:00 Asia/Taipei on the target day**.

Machine-readable outputs:

- `data/analysis/global_market_data_audit.json`: complete candidate/source audit.
- `config/global_market_registry.json`: disabled, experimental candidate feature definitions only.

## Existing project data

The project already has production or daily-flow support for:

- Taiwan: TAIEX, TPEx, TX after-hours, institutional flows, foreign futures positions, margin trading, breadth, and full TWSE/TPEx stock data.
- US/global: S&P 500, NASDAQ Composite, SOX, TSM ADR, VIX, and KOSPI.
- Research/history candidates: Nikkei 225, Hang Seng, CSI 300, SOXX, SMH, NVDA, AMD, AVGO, USD/TWD, DXY, US 2Y, US 10Y, and 10Y-2Y spread.
- Infrastructure: Provider Registry, historical augmentation scripts, temporal leakage audit, trading calendar, PostgreSQL stock tables, and REST API.

The V3.13 universe must reuse these providers and helpers rather than create parallel implementations.

## Audited universe

The audit contains **108 candidates** across Taiwan, US, Asia, Europe, other major markets, semiconductor/technology, commodities, FX, rates, volatility, crypto, ETF proxies, and macro.

Source priority:

1. Government, exchange, or central-bank official source.
2. FRED/ALFRED or another trusted public statistical database.
3. Official API.
4. Stable free market-data source with acceptable terms.
5. Yahoo Finance only as a reasonable quote source or fallback.

Important verified references:

- [TWSE OpenAPI](https://openapi.twse.com.tw/) publishes an OAS-described official API for cash-market datasets.
- [FRED observations API](https://fred.stlouisfed.org/docs/api/fred/series_observations.html) supports observations, real-time periods, initial releases, and vintage dates. Backtests must use ALFRED-style point-in-time availability rather than today's revised history.
- [FRED real-time periods](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html) distinguish what was known at a historical time.
- [Cboe VIX historical data](https://www.cboe.com/tradable_products/vix/vix_historical_data) supplies official daily VIX history from 1990 and selected volatility indices.
- [ECB SDMX API](https://data.ecb.europa.eu/help/getting-data-web-services-sdmx-0) offers official programmatic statistical data and metadata.
- [Japan e-Stat API](https://www.e-stat.go.jp/api/en) is official and machine-readable, but registration is required.
- [Coinbase public market API](https://docs.cdp.coinbase.com/exchange/introduction/welcome) is suitable for timestamped BTC/ETH research subject to its market-data terms and public rate limits.

### History tiers

Likely 20+ year instruments include major US indices, many developed-market indices, VIX, most core FX pairs, Treasury yields, major commodity futures, long-lived ETFs, and most US macro series.

Shorter-history candidates include:

- CSI 300 (2005), USD/CNH (2010), VIX9D (about 2011), BTC (usable exchange history from about 2015), ETH (about 2016), TX after-hours (2017), and newer ETFs.
- MOVE is deferred because a reliable free source with clear redistribution rights was not confirmed.
- Iron ore is deferred until a stable licensed source and continuous-contract policy are confirmed.
- Actual ETF fund flows/NAV subscriptions are not replaced by price/volume. High-liquidity ETF returns and volume are explicitly labelled proxies.

Historical start values in the audit are instrument/series inception estimates. Phase 2 must probe actual endpoint coverage before promising a backfill range.

## Temporal alignment

### Universal rule

For target trading day D:

```
feature.available_at < D 09:00:00 Asia/Taipei
```

Store both the economic/trading date and the availability timestamp. A date alone is not proof of availability.

| Data type | Required rule |
|---|---|
| US/Europe close | Latest completed local session before D open. |
| Japan/Korea/Hong Kong/China | Never use D close; these sessions complete after Taiwan opens. Use the latest session completed before D 09:00. |
| Australia | Determine using exact session timestamp and daylight-saving offset; do not infer from date. |
| TX night | For D, use the session ending around 05:00 Taipei on D; TAIFEX may label it D. |
| Crypto | Freeze at 09:00 Taipei (01:00 UTC); build 1h/4h/12h/24h returns only from timestamps before the cutoff. |
| FX | Use a fixed pre-open snapshot or a published reference rate whose `available_at` precedes D open. Do not mix reference and tradable rates. |
| Macro | Join on actual release timestamp and point-in-time vintage. Observation month/quarter is not availability. |
| Financials | Use `available_date`, never fiscal `period_end`. |
| Futures | Save contract and roll metadata; continuous-series roll jumps are not economic returns. |

Holiday mismatches require independent source calendars. “Previous calendar day” and “same date” joins are prohibited.

## Proposed PostgreSQL design

Keep global market data separate from existing `stock_*` tables. Existing stock tables are entity-oriented product data; global series need multi-source instrument identity, point-in-time availability, intraday timestamps, and revisions.

No production migration is performed in this phase.

### `data_sources`

- PK: `source_id`
- Unique: `source_key`
- Fields: name, official, base_url, transport, authentication type, license notes, rate-limit metadata, active, timestamps.
- Index: active, source_key.

### `market_instruments`

- PK: `instrument_id`
- Unique: `(source_id, source_symbol)`
- Fields: canonical_symbol, name, category, market, country, exchange, currency, timezone, frequency, trading hours, adjusted policy, inception date, active, metadata.
- Indexes: canonical_symbol, category, market/country.

### `market_daily_prices`

- PK: `(instrument_id, trade_date, source_id)`
- Fields: OHLCV, adjusted_close, currency, session, available_at, source_updated_at, quality flags, metadata.
- Indexes: `(instrument_id, trade_date DESC)`, `available_at`.
- UPSERT preserves nulls and source identity.

### `market_intraday_prices`

- PK: `(instrument_id, timestamp_utc, interval, source_id)`
- Fields: OHLCV, available_at, session, metadata.
- Partition recommendation: monthly or yearly by timestamp only when volume justifies it.
- Index: `(instrument_id, interval, timestamp_utc DESC)`.

### `macro_series`

- PK: `macro_series_id`
- Unique: `(source_id, source_series_id)`
- Fields: name, country, frequency, unit, seasonal adjustment, timezone, release calendar metadata, inception date.

### `macro_observations`

- PK: `(macro_series_id, observation_period, vintage_date)`
- Fields: numeric/text value, available_at, revision sequence, preliminary/final flag, source_updated_at, metadata.
- Indexes: `(macro_series_id, available_at DESC)`, observation period.
- Point-in-time queries select the latest vintage available before the target cutoff.

### `market_features`

- PK: `(feature_key, feature_timestamp, target_scope_key, registry_version)`
- Fields: value, source observation keys, calculated_at, available_at, transform version, quality flags.
- This is a derived cache, not the source of truth.

Raw payloads should not be duplicated into every row. Store immutable compressed raw files in object/file storage with checksum and ingestion-run reference if an audit trail is required.

## Candidate Feature Registry

The registry separates data acquisition from model selection. Required fields are:

- `feature_key`, display name, category, source series.
- transform and lag rule.
- explicit availability rule.
- target scope.
- enabled/experimental state.
- model version and inception date.

Every V3.13 entry is currently `enabled=false` and `experimental=true`; merely adding BVSP, BTC, or Copper never changes a production model.

## Feature-selection gate

```
Candidate
→ Temporal Leakage Audit
→ Missing Rate
→ Correlation
→ Mutual Information
→ Expanding-window Walk-forward
→ Leave-One-Out / Add-One-In
→ Coefficient / Importance Stability
→ Regime Validation
→ Calibration
→ Formal Feature
```

High correlation does not mean data should be discarded from the data lake. It means the feature may not deserve entry into a parsimonious production model.

## Capacity estimates

Assumptions: about 80 daily series, two hourly crypto series, selected macro observations/vintages, normalized numeric rows with PostgreSQL indexes, and no tick/raw-payload archive.

| Horizon | Approx. rows | PostgreSQL estimate | Initial backfill | Daily normalized writes |
|---|---:|---:|---:|---:|
| 5 years | 198k | 70–140 MB | 1–4 hours | 90–250 |
| 10 years | 395k | 140–300 MB | 3–8 hours | 90–250 |
| 20 years | 790k | 280–600 MB | 6–20 hours | 90–250 |
| Maximum reliable | 0.9–1.8m | 450 MB–1.2 GB | 1–3 days | 90–250 |

Intraday 1-minute crypto or raw API archives change this materially and should have an explicit retention/aggregation policy. Hourly crypto is enough to construct the requested pre-open windows.

Different features retain different inception dates. Research uses cohorts or availability masks; a 2015 BTC feature must not truncate a 20-year index/rates dataset.

## Recommended Phase 2

First backfill batch:

1. Replace historical VIX with the official Cboe file and retain Yahoo as latest/fallback.
2. Add Nasdaq 100, Russell 2000, and EWT.
3. Add USD/JPY, USD/KRW, and USD/CNH with a fixed timestamp policy.
4. Add Copper, WTI, and Gold with continuous-contract/roll metadata.
5. Add Coinbase BTC/USD and ETH/USD hourly data and pre-open 1h/4h/12h/24h features.
6. Add FRED DGS5, DGS30, T10Y3M, and ICSA using point-in-time availability.

Second batch: KOSDAQ, ASX 200, TOPIX, QQQ/XLK/EWY/EEM, Brent, natural gas, EUR/USD, AUD/USD, VIX9D/VIX3M, and a small number of point-in-time US macro releases.

Do not start with the entire universe. Phase 2 should implement source adapters, checkpointed sample backfills, availability validation, missing-rate reports, and a non-production research schema/migration proposal. Only after that should the project approve a production migration or model experiment.

## Main conclusions

- The project already owns much of the Taiwan/US/semiconductor foundation; duplicate providers would create inconsistent alignment.
- The highest-value gap is not “more indices”; it is timestamp-correct FX, commodities, official VIX history, crypto pre-open windows, and point-in-time macro releases.
- Maximum reliable history is affordable on the current PostgreSQL scale if raw/tick archives are controlled.
- The primary risks are future leakage, licensing, revised macro history, continuous-futures rolls, and cross-market holiday alignment.
