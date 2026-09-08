"""Safely backfill missing production market-history sessions from dated sources."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import shutil
import ssl
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from requests.adapters import HTTPAdapter

from analysis.market_signal_engine import MarketSignalEngine
from backfill_history import (
    TAIFEX_NIGHT_URL,
    RateLimitedSession,
    SourceError,
    _NightMarketParser,
    fetch_foreign_cash,
    fetch_foreign_futures,
    fetch_tpex_month,
    fetch_tpex_quotes,
    fetch_twse_market,
    fetch_yahoo_history,
    latest_before,
)
from config import MARKET_DATA_DIR, PROJECT_ROOT
from prediction_temporal import prediction_target_date
from providers.taifex_night_futures_provider import TaifexNightFuturesProvider
from trading_calendar import load_trading_calendar
from update_history import (
    MARKET_FIELDS,
    SIGNAL_NAMES,
    SIGNALS_FIELDS,
    MARKET_HISTORY_PATH,
    SIGNALS_HISTORY_PATH,
    validate_prediction_dates,
)


LOGGER = logging.getLogger("market_history_gap_backfill")
YAHOO_SOURCES = {
    "tsm_adr": "TSM",
    "sox": "^SOX",
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
    "vix": "^VIX",
    "kospi": "^KS11",
}


class CompatibleCertificateAdapter(HTTPAdapter):
    """Keep TLS verification while tolerating TPEx's legacy certificate chain."""

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        context = ssl.create_default_context()
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs["ssl_context"] = context
        super().init_poolmanager(*args, **kwargs)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=30)
    return parser.parse_args()


def official_sessions(start: date, end: date) -> list[date]:
    if start > end:
        raise ValueError("--start must not be later than --end")
    calendar = load_trading_calendar()
    sessions: list[date] = []
    current = start
    while current <= end:
        year = calendar["years"].get(str(current.year))
        if not isinstance(year, dict):
            raise ValueError(f"Trading calendar does not cover {current.year}")
        iso = current.isoformat()
        weekdays = {int(value) for value in year["regular_trading_weekdays"]}
        if iso in set(year["special_open_dates"]) or (
            current.isoweekday() in weekdays and iso not in set(year["closed_dates"])
        ):
            sessions.append(current)
        current = date.fromordinal(current.toordinal() + 1)
    return sessions


def load_csv(path: Path, fields: tuple[str, ...]) -> dict[str, dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if tuple(reader.fieldnames or ()) != fields:
            raise ValueError(f"Unexpected CSV header: {path}")
        rows = list(reader)
    indexed: dict[str, dict[str, str]] = {}
    for row in rows:
        key = row.get("trade_date", "")
        if not key or key in indexed:
            raise ValueError(f"Missing or duplicate trade_date in {path}: {key}")
        indexed[key] = row
    if list(indexed) != sorted(indexed):
        raise ValueError(f"Dates are not sorted in {path}")
    return indexed


def fetch_night_record(http: RateLimitedSession, trade_date: date) -> dict[str, Any]:
    response = http.request(
        "POST",
        TAIFEX_NIGHT_URL,
        data={
            "queryType": "2", "marketCode": "1", "MarketCode": "1",
            "dateaddcnt": "", "commodity_id": "TX", "commodity_id2": "",
            "queryDate": trade_date.strftime("%Y/%m/%d"),
        },
    )
    response.encoding = "utf-8"
    parser = _NightMarketParser()
    parser.feed(response.text)
    provider = TaifexNightFuturesProvider()
    if parser.query_date != trade_date.strftime("%Y/%m/%d"):
        raise SourceError("TAIFEX night response date mismatch")
    records = provider.normalize({"trade_date": trade_date.isoformat(), "rows": parser.rows})
    if not provider.validate(records):
        raise SourceError("TAIFEX night futures validation failed")
    return records[0]


def payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {"data": {"records": [dict(record)]}}


def signal_row_for(market_row: Mapping[str, Any], records: Mapping[str, Any]) -> dict[str, Any]:
    config_path = PROJECT_ROOT / "config" / "factor_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    market_record = {
        "trade_date": market_row["trade_date"],
        "taiex": {"change_percent": market_row["taiex_change_percent"]},
        "tpex": {"change_percent": records["tpex_index"]["change_percent"]},
        "advancing": market_row["advancing"],
        "declining": market_row["declining"],
    }
    sources = {
        "factor_config": config,
        "taiwan_market_overview": payload(market_record),
        "institutional_investors": payload({
            "trade_date": market_row["institutional_trade_date"],
            "foreign_and_mainland_investors": {"net": market_row["foreign_cash_flow"]},
        }),
        "foreign_futures_position": payload({
            "trade_date": market_row["foreign_futures_trade_date"],
            "net_position": {"open_interest": market_row["foreign_futures_position"]},
        }),
        "night_futures": payload(records["night_record"]),
        "tsm_adr": payload({"trade_date": market_row["tsm_adr_trade_date"], "change_percent": market_row["tsm_adr_change_percent"]}),
        "sox_index": payload({"trade_date": market_row["sox_trade_date"], "change_percent": market_row["sox_change_percent"]}),
        "sp500_index": payload({"trade_date": market_row["sp500_trade_date"], "change_percent": market_row["sp500_change_percent"]}),
        "nasdaq_index": payload({"trade_date": market_row["nasdaq_trade_date"], "change_percent": market_row["nasdaq_change_percent"]}),
        "vix_index": payload({"trade_date": market_row["vix_trade_date"], "change_percent": market_row["vix_change_percent"]}),
        "kospi_index": payload({"trade_date": market_row["kospi_trade_date"], "change_percent": market_row["kospi_change_percent"]}),
    }
    result = MarketSignalEngine(MARKET_DATA_DIR, config_path).analyze(sources)
    row: dict[str, Any] = {
        "trade_date": market_row["trade_date"],
        **{field: market_row[field] for field in SIGNALS_FIELDS if field.endswith("_trade_date")},
    }
    for name in SIGNAL_NAMES:
        rule = result["rules"][name]
        if not rule["available"] or rule["score"] is None:
            raise ValueError(f"Historical signal is unavailable: {name}")
        row[f"{name}_status"] = rule["status"]
        row[f"{name}_score"] = rule["score"]
    score = result["market_score"]
    row.update(market_score=score["score"], max_score=score["max_score"], percentage=score["percentage"], market_status=score["status"])
    return row


def build_gap_rows(start: date, end: date, missing: list[date], http: RateLimitedSession) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    tpex_months: dict[str, dict[str, Any]] = {}
    for year, month in sorted({(item.year, item.month) for item in missing}):
        tpex_months.update(fetch_tpex_month(http, year, month))
    yahoo = {
        name: fetch_yahoo_history(http, symbol, start, end)
        for name, symbol in YAHOO_SOURCES.items()
    }
    market_rows: dict[str, dict[str, Any]] = {}
    signal_rows: dict[str, dict[str, Any]] = {}
    report: dict[str, Any] = {"recoverable_dates": [], "unrecoverable_dates": [], "source_date_mapping": {}}
    for trade_date in missing:
        key = trade_date.isoformat()
        target_text = prediction_target_date(key)
        failures: dict[str, str] = {}
        if target_text is None:
            report["unrecoverable_dates"].append({"trade_date": key, "missing": {"calendar": "target unavailable"}})
            continue
        target = date.fromisoformat(target_text)
        values: dict[str, Any] = {}
        fetchers = {
            "twse_market": lambda: fetch_twse_market(http, trade_date),
            "tpex_quotes": lambda: fetch_tpex_quotes(http, trade_date),
            "institutional": lambda: fetch_foreign_cash(http, trade_date),
            "foreign_futures": lambda: fetch_foreign_futures(http, trade_date),
            "night_record": lambda: fetch_night_record(http, target),
        }
        for name, fetcher in fetchers.items():
            try:
                values[name] = fetcher()
            except Exception as exc:
                failures[name] = str(exc)
        values["tpex_index"] = tpex_months.get(key)
        if values["tpex_index"] is None:
            failures["tpex_index"] = "official monthly record is missing"
        yahoo_values: dict[str, tuple[date, Mapping[str, Any]]] = {}
        for name, history in yahoo.items():
            try:
                yahoo_values[name] = latest_before(history, target, name)
            except Exception as exc:
                failures[name] = str(exc)
        if failures:
            report["unrecoverable_dates"].append({"trade_date": key, "missing": failures})
            continue
        twse, tpex = values["twse_market"], values["tpex_quotes"]
        source_dates = {
            "taiwan_market": key, "institutional": key, "foreign_futures": key,
            "night_futures": target_text,
            **{name: item[0].isoformat() for name, item in yahoo_values.items()},
        }
        validate_prediction_dates(source_dates, key)
        row: dict[str, Any] = {
            "trade_date": key,
            "taiwan_market_trade_date": key,
            "institutional_trade_date": key,
            "foreign_futures_trade_date": key,
            "night_futures_trade_date": target_text,
            "tsm_adr_trade_date": source_dates["tsm_adr"],
            "sox_trade_date": source_dates["sox"],
            "sp500_trade_date": source_dates["sp500"],
            "nasdaq_trade_date": source_dates["nasdaq"],
            "taiex_close": twse["close"], "taiex_change_percent": twse["change_percent"],
            "tpex_close": values["tpex_index"]["close"],
            "turnover": twse["turnover"] + tpex["turnover"],
            "advancing": twse["advancing"] + tpex["advancing"],
            "declining": twse["declining"] + tpex["declining"],
            "unchanged": twse["unchanged"] + tpex["unchanged"],
            "foreign_cash_flow": values["institutional"],
            "foreign_futures_position": values["foreign_futures"],
            "night_futures_change": values["night_record"]["change"],
            "tsm_adr_change_percent": yahoo_values["tsm_adr"][1]["change_percent"],
            "sox_change_percent": yahoo_values["sox"][1]["change_percent"],
            "sp500_change_percent": yahoo_values["sp500"][1]["change_percent"],
            "nasdaq_change_percent": yahoo_values["nasdaq"][1]["change_percent"],
            "vix_trade_date": source_dates["vix"], "vix_change_percent": yahoo_values["vix"][1]["change_percent"],
            "kospi_trade_date": source_dates["kospi"], "kospi_change_percent": yahoo_values["kospi"][1]["change_percent"],
        }
        if set(row) != set(MARKET_FIELDS) or any(row[field] in (None, "") for field in MARKET_FIELDS):
            report["unrecoverable_dates"].append({"trade_date": key, "missing": {"validation": "incomplete market row"}})
            continue
        try:
            signal_row = signal_row_for(row, values)
        except Exception as exc:
            report["unrecoverable_dates"].append({"trade_date": key, "missing": {"signal_engine": str(exc)}})
            continue
        market_rows[key], signal_rows[key] = row, signal_row
        report["recoverable_dates"].append(key)
        report["source_date_mapping"][key] = {"target_date": target_text, **source_dates}
    return market_rows, signal_rows, report


def validate_merged(rows: Mapping[str, Mapping[str, Any]], fields: tuple[str, ...], *, temporal: bool) -> None:
    keys = list(sorted(rows))
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate trade_date after merge")
    for key in keys:
        row = rows[key]
        if tuple(row.keys()) != fields and set(row) != set(fields):
            raise ValueError(f"Schema mismatch for {key}")
        if str(row["trade_date"]) != key:
            raise ValueError(f"Trade-date key mismatch for {key}")
        if temporal:
            validate_prediction_dates(
                {
                    "taiwan_market": str(row["taiwan_market_trade_date"]),
                    "institutional": str(row["institutional_trade_date"]),
                    "foreign_futures": str(row["foreign_futures_trade_date"]),
                    "night_futures": str(row["night_futures_trade_date"]),
                    "tsm_adr": str(row["tsm_adr_trade_date"]), "sox": str(row["sox_trade_date"]),
                    "sp500": str(row["sp500_trade_date"]), "nasdaq": str(row["nasdaq_trade_date"]),
                    "vix": str(row["vix_trade_date"]), "kospi": str(row["kospi_trade_date"]),
                }, key
            )


def write_temp(path: Path, fields: tuple[str, ...], rows: Mapping[str, Mapping[str, Any]]) -> Path:
    temporary = path.with_suffix(f"{path.suffix}.backfill.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="raise", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows[key] for key in sorted(rows))
        output.flush()
        os.fsync(output.fileno())
    return temporary


def apply_rows(market_new: Mapping[str, Mapping[str, Any]], signals_new: Mapping[str, Mapping[str, Any]]) -> tuple[int, int, list[Path]]:
    market_existing = load_csv(MARKET_HISTORY_PATH, MARKET_FIELDS)
    signals_existing = load_csv(SIGNALS_HISTORY_PATH, SIGNALS_FIELDS)
    market_merged = {**market_existing, **market_new}
    signals_merged = {**signals_existing, **signals_new}
    validate_merged(market_merged, MARKET_FIELDS, temporal=True)
    validate_merged(signals_merged, SIGNALS_FIELDS, temporal=False)
    market_temp = write_temp(MARKET_HISTORY_PATH, MARKET_FIELDS, market_merged)
    signals_temp = write_temp(SIGNALS_HISTORY_PATH, SIGNALS_FIELDS, signals_merged)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backups = [Path(f"{MARKET_HISTORY_PATH}.{stamp}.bak"), Path(f"{SIGNALS_HISTORY_PATH}.{stamp}.bak")]
    shutil.copy2(MARKET_HISTORY_PATH, backups[0])
    shutil.copy2(SIGNALS_HISTORY_PATH, backups[1])
    try:
        os.replace(market_temp, MARKET_HISTORY_PATH)
        os.replace(signals_temp, SIGNALS_HISTORY_PATH)
    except Exception:
        shutil.copy2(backups[0], MARKET_HISTORY_PATH)
        shutil.copy2(backups[1], SIGNALS_HISTORY_PATH)
        raise
    finally:
        for temporary in (market_temp, signals_temp):
            if temporary.exists():
                temporary.unlink()
    return len(market_merged) - len(market_existing), len(signals_merged) - len(signals_existing), backups


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    args = parse_args()
    expected = official_sessions(args.start, args.end)
    market_existing = load_csv(MARKET_HISTORY_PATH, MARKET_FIELDS)
    signals_existing = load_csv(SIGNALS_HISTORY_PATH, SIGNALS_FIELDS)
    missing = [item for item in expected if item.isoformat() not in market_existing]
    missing_signals = [item.isoformat() for item in expected if item.isoformat() not in signals_existing]
    http = RateLimitedSession(args.delay, args.retries, args.timeout)
    http.session.mount("https://www.tpex.org.tw", CompatibleCertificateAdapter())
    work_dates = sorted({*missing, *(date.fromisoformat(item) for item in missing_signals)})
    if work_dates:
        market_built, signals_built, report = build_gap_rows(
            args.start, args.end, work_dates, http
        )
    else:
        market_built, signals_built = {}, {}
        report = {
            "recoverable_dates": [],
            "unrecoverable_dates": [],
            "source_date_mapping": {},
        }
    market_new = {item.isoformat(): market_built[item.isoformat()] for item in missing if item.isoformat() in market_built}
    signals_new = {item: signals_built[item] for item in missing_signals if item in signals_built}
    summary = {
        "mode": "dry-run" if args.dry_run else "apply",
        "expected_dates": [item.isoformat() for item in expected],
        "missing_dates": [item.isoformat() for item in missing],
        "missing_signal_dates": missing_signals,
        **report,
    }
    if args.apply:
        market_added, signals_added, backups = apply_rows(market_new, signals_new)
        summary.update(market_rows_added=market_added, signal_rows_added=signals_added, backups=[str(path) for path in backups])
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not report["unrecoverable_dates"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, SourceError) as exc:
        LOGGER.error("Market history gap backfill failed: %s", exc)
        raise SystemExit(1)
