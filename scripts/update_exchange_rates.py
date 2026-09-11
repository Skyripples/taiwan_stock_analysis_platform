"""Fetch daily TWD exchange rates from the Central Bank of Taiwan."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import xlrd

from update_bank_rates import CBCAdapter, session
from io_utils import atomic_write_json


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "forex" / "exchange_rates.json"
SOURCE_URL = "https://cpx.cbc.gov.tw/API/DataAPI/Get?FileName=BP01D01"
DAILY_XLS_URL = "https://www.cbc.gov.tw/Public/Data/economic/statistic/fx/%E6%97%A5%E8%B3%87%E6%96%99%20(2002%E5%B9%B4%E8%BF%84%E4%BB%8A).xls"
LOGGER = logging.getLogger("exchange-rates")

CURRENCIES = {
    "USD": {"name": "美元", "column": 0, "mode": "base"},
    "JPY": {"name": "日圓", "column": 1, "mode": "divide"},
    "EUR": {"name": "歐元", "column": 13, "mode": "multiply"},
    "CNY": {"name": "人民幣", "column": 7, "mode": "divide"},
    "GBP": {"name": "英鎊", "column": 2, "mode": "multiply"},
    "AUD": {"name": "澳幣", "column": 8, "mode": "multiply"},
    "CAD": {"name": "加拿大幣", "column": 5, "mode": "divide"},
    "SGD": {"name": "新加坡幣", "column": 6, "mode": "divide"},
    "HKD": {"name": "港幣", "column": 3, "mode": "divide"},
    "KRW": {"name": "韓元", "column": 4, "mode": "divide"},
}


def number(value: Any) -> float | None:
    try:
        result = float(str(value).replace(",", "").strip())
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None


def fetch(client: requests.Session | None = None) -> dict[str, Any]:
    client = client or session()
    response = client.get(SOURCE_URL, timeout=(10, 35))
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data", {}).get("dataSets")
    if not isinstance(rows, list) or not rows:
        raise ValueError("CBC returned no exchange-rate rows")
    # The API export can lag behind the official daily workbook. Merge newer
    # official workbook rows without assigning a newer date to an older quote.
    try:
        client.mount("https://www.cbc.gov.tw", CBCAdapter())
        workbook_response = client.get(DAILY_XLS_URL, timeout=(10, 35))
        workbook_response.raise_for_status()
        workbook = xlrd.open_workbook(file_contents=workbook_response.content)
        sheet = workbook.sheet_by_index(0)
        xls_to_api = {2: 0, 3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 6, 9: 7, 10: 8, 15: 13}
        known_dates = {str(row[0]) for row in rows if isinstance(row, list) and row}
        for row_index in range(7, sheet.nrows):
            excel_date = sheet.cell_value(row_index, 1)
            if not isinstance(excel_date, (int, float)) or excel_date <= 0:
                continue
            date_key = xlrd.xldate_as_datetime(excel_date, workbook.datemode).strftime("%Y%m%d")
            if date_key in known_dates:
                continue
            merged = [date_key, *("-" for _ in range(18))]
            for xls_column, api_column in xls_to_api.items():
                merged[api_column + 1] = sheet.cell_value(row_index, xls_column)
            rows.append(merged)
            known_dates.add(date_key)
    except Exception as exc:
        LOGGER.warning("CBC daily workbook unavailable; using API observations only: %s", exc)
    return payload


def normalize(raw: dict[str, Any], fetched_at: datetime | None = None) -> dict[str, Any]:
    rows = raw.get("data", {}).get("dataSets", [])
    rates = []
    for code, definition in CURRENCIES.items():
        # A currency can be temporarily absent while the other columns keep
        # updating. Select its own newest row where both that quote and the
        # same-day NTD/USD cross-rate are valid.
        candidates = [
            row for row in rows
            if isinstance(row, list)
            and len(row) > definition["column"] + 1
            and str(row[0]).isdigit()
            and number(row[1]) is not None
            and number(row[definition["column"] + 1]) is not None
        ]
        if not candidates:
            raise ValueError(f"CBC returned no valid observation for {code}")
        latest = max(candidates, key=lambda row: str(row[0]))
        source_date_raw = str(latest[0])
        source_date = f"{source_date_raw[:4]}-{source_date_raw[4:6]}-{source_date_raw[6:8]}"
        twd_per_usd = number(latest[1])
        quoted = number(latest[definition["column"] + 1])
        if definition["mode"] == "base":
            twd_rate = twd_per_usd
        elif quoted is None:
            twd_rate = None
        elif definition["mode"] == "multiply":
            twd_rate = twd_per_usd * quoted
        else:
            twd_rate = twd_per_usd / quoted
        rates.append({
            "currency": code,
            "name": definition["name"],
            "twd_rate": round(twd_rate, 8) if twd_rate is not None else None,
            "source_date": source_date,
        })

    source_dates = {item["source_date"] for item in rates}
    common_date = next(iter(source_dates)) if len(source_dates) == 1 else None
    latest_source_date = max(source_dates)
    timestamp = (fetched_at or datetime.now(timezone.utc)).isoformat()
    return {
        "updated_at": timestamp,
        "provider": "CBC",
        "dataset": "twd_exchange_rates",
        "version": "1.0",
        "data_date": common_date,
        "latest_source_date": latest_source_date,
        "fetched_at": timestamp,
        "source": DAILY_XLS_URL,
        "sources": [SOURCE_URL, DAILY_XLS_URL],
        "base_currency": "TWD",
        "unit": "TWD per 1 currency unit",
        "rates": rates,
    }


def validate(payload: dict[str, Any]) -> None:
    if payload.get("provider") != "CBC" or payload.get("dataset") != "twd_exchange_rates":
        raise ValueError("Invalid exchange-rate metadata")
    rates = payload.get("rates")
    if not isinstance(rates, list) or {item.get("currency") for item in rates} != set(CURRENCIES):
        raise ValueError("Exchange-rate currency coverage is incomplete")
    if any(item.get("twd_rate") is None or item["twd_rate"] <= 0 for item in rates):
        raise ValueError("Exchange-rate payload contains invalid values")
    source_dates = {item.get("source_date") for item in rates}
    expected_common_date = next(iter(source_dates)) if len(source_dates) == 1 else None
    if payload.get("data_date") != expected_common_date or payload.get("latest_source_date") != max(source_dates):
        raise ValueError("Exchange-rate top-level dates are inconsistent")


def write_atomic(payload: dict[str, Any], output: Path = OUTPUT) -> None:
    atomic_write_json(output, payload)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    try:
        payload = normalize(fetch())
        validate(payload)
        if OUTPUT.exists():
            existing = json.loads(OUTPUT.read_text(encoding="utf-8"))
            existing_date = existing.get("latest_source_date") or existing.get("data_date") or existing.get("source_date")
            if existing_date and payload["latest_source_date"] < existing_date:
                raise ValueError(f"source regressed from {existing_date} to {payload['latest_source_date']}")
        write_atomic(payload)
    except Exception as exc:
        LOGGER.error("Exchange-rate update failed; existing valid JSON was preserved: %s", exc)
        return 1
    LOGGER.info("Exchange rates updated | latest_source_date=%s | currencies=%d", payload["latest_source_date"], len(payload["rates"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
