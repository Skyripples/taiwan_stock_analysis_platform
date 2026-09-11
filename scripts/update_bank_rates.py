"""Fetch current Taiwan bank deposit board rates from the CBC BIRWEB service."""
from __future__ import annotations

import json
import logging
import re
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from io_utils import atomic_write_json


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "deposits" / "bank_rates.json"
BASE_URL = "https://cpx.cbc.gov.tw/BIRWEB"
SOURCE_URL = f"{BASE_URL}/"
TERMS = {"1月": "1M", "3月": "3M", "6月": "6M", "9月": "9M", "1年": "1Y", "2年": "2Y", "3年": "3Y"}
LOGGER = logging.getLogger("bank-rates")


class CBCAdapter(HTTPAdapter):
    """Keep certificate verification while tolerating the CBC legacy chain on Python 3.13."""

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        context = ssl.create_default_context()
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs["ssl_context"] = context
        super().init_poolmanager(*args, **kwargs)


def session() -> requests.Session:
    client = requests.Session()
    retries = Retry(total=2, backoff_factor=0.8, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET", "POST"}))
    client.mount("https://cpx.cbc.gov.tw", CBCAdapter(max_retries=retries))
    client.headers.update({"User-Agent": "taiwan-stock-analysis-platform/3.18", "Accept": "application/json"})
    return client


def request_json(client: requests.Session, path: str, *, payload: dict[str, Any] | None = None) -> Any:
    response = client.post(f"{BASE_URL}/{path}", json=payload, timeout=(10, 35))
    response.raise_for_status()
    value = response.json()
    return json.loads(value) if isinstance(value, str) else value


def fetch(client: requests.Session | None = None) -> tuple[dict[str, Any], str]:
    client = client or session()
    metadata = request_json(client, "Range/IGetJsonRangeData")
    bank_values = next((item.get("Value", []) for item in metadata.get("values", []) if item.get("Key") == "銀行名稱"), [])
    banks = [str(name).strip() for name in bank_values if str(name).strip()]
    if not banks:
        raise ValueError("CBC returned no bank names")
    payload = {
        "_range": {"values": [{"key": "銀行名稱", "value": banks}]},
        "BeginDate": None, "EndDate": None, "RateSort": None,
    }
    response = request_json(client, "Data/IGetJsonFromArray", payload=payload)
    rows = response.get("data")
    if not isinstance(rows, list) or not rows:
        raise ValueError("CBC returned no deposit rate rows")
    landing = client.get(f"{BASE_URL}/Data/IDataMain", timeout=(10, 25))
    landing.raise_for_status()
    match = re.search(r"查詢日期\s*:\s*(\d{4}-\d{2}-\d{2})", landing.text)
    if not match:
        raise ValueError("CBC source date is missing")
    source_date = match.group(1)
    return response, source_date


def number(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text or text == "--":
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def empty_terms(keys: list[str]) -> dict[str, dict[str, float | None]]:
    return {key: {"fixed": None, "variable": None} for key in keys}


def normalize(raw: dict[str, Any], source_date: str) -> dict[str, Any]:
    banks: dict[str, dict[str, Any]] = {}
    for row in raw.get("data", []):
        if not isinstance(row, list) or len(row) < 6:
            continue
        raw_bank, item, duration, tier, fixed, variable = (str(value or "").strip() for value in row[:6])
        bank_code_match = re.match(r"^(\d{3})", raw_bank)
        bank_code = bank_code_match.group(1) if bank_code_match else None
        bank_name = re.sub(r"\s+", "", re.sub(r"^\d{3}", "", raw_bank)).strip()
        if not bank_name or tier not in ("", "一般"):
            continue
        bank = banks.setdefault(bank_name, {
            "bank_name": bank_name, "bank_code": bank_code,
            "demand": {"demand_deposit": None, "savings_deposit": None},
            "time_deposit": empty_terms(["1M", "3M", "6M", "9M", "1Y", "2Y", "3Y"]),
            "time_savings_deposit": empty_terms(["1Y", "2Y", "3Y"]),
        })
        fixed_rate, variable_rate = number(fixed), number(variable)
        if item == "活期存款":
            bank["demand"]["demand_deposit"] = variable_rate if variable_rate is not None else fixed_rate
        elif item == "活期儲蓄存款":
            bank["demand"]["savings_deposit"] = variable_rate if variable_rate is not None else fixed_rate
        elif duration in TERMS and item == "定期存款":
            bank["time_deposit"][TERMS[duration]] = {"fixed": fixed_rate, "variable": variable_rate}
        elif duration in TERMS and item == "定期儲蓄存款" and TERMS[duration] in bank["time_savings_deposit"]:
            bank["time_savings_deposit"][TERMS[duration]] = {"fixed": fixed_rate, "variable": variable_rate}
    usable = [bank for bank in banks.values() if bank["demand"]["demand_deposit"] is not None or any(v["fixed"] is not None or v["variable"] is not None for v in bank["time_deposit"].values())]
    usable.sort(key=lambda item: item["bank_name"])
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": "CBC",
        "dataset": "bank_deposit_rates",
        "version": "1.0",
        "status": "success",
        "source_date": source_date,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": SOURCE_URL,
        "unit": "percent_per_annum",
        "banks": usable,
    }


def validate(payload: dict[str, Any]) -> None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(payload.get("source_date", ""))):
        raise ValueError("invalid source_date")
    banks = payload.get("banks")
    if not isinstance(banks, list) or not banks:
        raise ValueError("no usable bank rates")
    if len({bank["bank_name"] for bank in banks}) != len(banks):
        raise ValueError("duplicate bank_name")


def write_atomic(payload: dict[str, Any], output: Path = OUTPUT) -> None:
    atomic_write_json(output, payload)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    try:
        raw, source_date = fetch()
        payload = normalize(raw, source_date)
        validate(payload)
        write_atomic(payload)
        LOGGER.info("CBC bank rates updated | banks=%d | source_date=%s", len(payload["banks"]), source_date)
        return 0
    except Exception as exc:
        LOGGER.error("CBC bank rates update failed; existing JSON preserved | %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
