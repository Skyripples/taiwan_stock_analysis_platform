"""Fetch Taiwan and U.S. government bond yield curves from official sources."""
from __future__ import annotations

import logging
import math
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests
import xlrd
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from io_utils import atomic_write_json


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "bonds" / "bond_yields.json"
TPEX_LIST_URL = "https://www.tpex.org.tw/www/zh-tw/bond/govDaily2"
TPEX_PAGE_URL = "https://www.tpex.org.tw/zh-tw/bond/info/statistics-gb/day/yield.html"
TREASURY_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
LOGGER = logging.getLogger("bond-yields")

US_FIELDS = {
    "3M": (3, "BC_3MONTH"), "6M": (6, "BC_6MONTH"), "1Y": (12, "BC_1YEAR"),
    "2Y": (24, "BC_2YEAR"), "5Y": (60, "BC_5YEAR"), "10Y": (120, "BC_10YEAR"),
    "20Y": (240, "BC_20YEAR"), "30Y": (360, "BC_30YEAR"),
}


def session() -> requests.Session:
    client = requests.Session()
    retry = Retry(total=2, backoff_factor=0.8, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET"}))
    client.mount("https://", HTTPAdapter(max_retries=retry))
    client.headers.update({"User-Agent": "taiwan-stock-analysis-platform/3.20", "Accept": "application/json, application/xml, */*"})
    return client


def number(value: Any) -> float | None:
    try:
        result = float(str(value).strip())
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def fetch_taiwan(client: requests.Session) -> dict[str, Any]:
    response = client.get(TPEX_LIST_URL, params={"date": date.today().strftime("%Y/%m/%d"), "fileCode": "Curve"}, timeout=(10, 35))
    response.raise_for_status()
    rows = response.json().get("tables", [{}])[0].get("data", [])
    if not rows or len(rows[0]) < 2:
        raise ValueError("TPEx returned no government yield-curve workbook")
    workbook_url = requests.compat.urljoin("https://www.tpex.org.tw", rows[0][1])
    workbook_response = client.get(workbook_url, timeout=(10, 35))
    workbook_response.raise_for_status()
    workbook = xlrd.open_workbook(file_contents=workbook_response.content)
    sheet = workbook.sheet_by_index(0)
    workbook_date = xlrd.xldate_as_datetime(sheet.cell_value(0, 2), workbook.datemode).date().isoformat()
    observations = []
    for row_index in range(1, sheet.nrows):
        tenor_text = str(sheet.cell_value(row_index, 1))
        match = re.search(r"(\d+)\s*年", tenor_text)
        value = number(sheet.cell_value(row_index, 2))
        if match and value is not None:
            years = int(match.group(1))
            observations.append({"tenor": f"{years}Y", "tenor_months": years * 12, "yield": value, "source_date": workbook_date})
    if not observations:
        raise ValueError("TPEx workbook contains no benchmark yields")
    return {"data_date": workbook_date, "source": TPEX_PAGE_URL, "download": workbook_url, "yields": observations}


def fetch_united_states(client: requests.Session) -> dict[str, Any]:
    response = client.get(TREASURY_URL, params={"data": "daily_treasury_yield_curve", "field_tdr_date_value": date.today().year}, timeout=(10, 35))
    response.raise_for_status()
    root = ET.fromstring(response.content)
    atom = "{http://www.w3.org/2005/Atom}"
    data_namespace = "{http://schemas.microsoft.com/ado/2007/08/dataservices}"
    entries = root.findall(f"{atom}entry")
    if not entries:
        raise ValueError("U.S. Treasury returned no yield-curve observations")

    parsed = []
    for entry in entries:
        values = {element.tag.removeprefix(data_namespace): element.text for element in entry.iter() if element.tag.startswith(data_namespace)}
        if values.get("NEW_DATE"):
            parsed.append((values["NEW_DATE"][:10], values))
    data_date, latest = max(parsed, key=lambda item: item[0])
    observations = []
    for tenor, (months, field) in US_FIELDS.items():
        value = number(latest.get(field))
        if value is not None:
            observations.append({"tenor": tenor, "tenor_months": months, "yield": value, "source_date": data_date})
    if not observations:
        raise ValueError("U.S. Treasury latest observation has no yields")
    return {"data_date": data_date, "source": TREASURY_URL, "yields": observations}


def spread_10y_2y(observations: list[dict[str, Any]]) -> float | None:
    values = {item["tenor"]: item["yield"] for item in observations}
    return round(values["10Y"] - values["2Y"], 4) if "2Y" in values and "10Y" in values else None


def build_payload(taiwan: dict[str, Any], united_states: dict[str, Any], fetched_at: datetime | None = None) -> dict[str, Any]:
    timestamp = (fetched_at or datetime.now(timezone.utc)).isoformat()
    markets = {
        "taiwan": {"name": "台灣政府公債", **taiwan},
        "united_states": {"name": "美國政府公債", **united_states},
    }
    for market in markets.values():
        market["spread_10y_2y"] = spread_10y_2y(market["yields"])
    return {"updated_at": timestamp, "fetched_at": timestamp, "provider": "TPEx / U.S. Treasury", "dataset": "government_bond_yields", "version": "1.0", "unit": "percent", "markets": markets}


def validate(payload: dict[str, Any]) -> None:
    if payload.get("dataset") != "government_bond_yields" or payload.get("unit") != "percent":
        raise ValueError("Invalid bond-yield metadata")
    markets = payload.get("markets")
    if not isinstance(markets, dict) or set(markets) != {"taiwan", "united_states"}:
        raise ValueError("Both government bond markets are required")
    for key, market in markets.items():
        observations = market.get("yields")
        if not isinstance(observations, list) or not observations:
            raise ValueError(f"No yields for {key}")
        months = [item.get("tenor_months") for item in observations]
        if len(months) != len(set(months)) or any(not isinstance(value, int) or value <= 0 for value in months):
            raise ValueError(f"Invalid tenors for {key}")
        if any(number(item.get("yield")) is None or item.get("source_date") != market.get("data_date") for item in observations):
            raise ValueError(f"Invalid values or dates for {key}")


def write_atomic(payload: dict[str, Any], output: Path = OUTPUT) -> None:
    atomic_write_json(output, payload)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    try:
        client = session()
        payload = build_payload(fetch_taiwan(client), fetch_united_states(client))
        validate(payload)
        write_atomic(payload)
    except Exception as exc:
        LOGGER.error("Bond-yield update failed; existing valid JSON was preserved: %s", exc)
        return 1
    LOGGER.info("Bond yields updated | Taiwan=%s | U.S.=%s", payload["markets"]["taiwan"]["data_date"], payload["markets"]["united_states"]["data_date"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
