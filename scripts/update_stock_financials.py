"""Build multi-quarter company financial caches from official MOPS reports.

The MOPS income statement exposes current-quarter amounts, while its cash-flow
statement exposes year-to-date amounts. Cash flows are therefore converted to
standalone quarters before OCF, CapEx, FCF, or TTM values are used downstream.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

import requests

from config import PROJECT_ROOT


LOGGER = logging.getLogger("stock_financials")
MOPS = "https://mopsov.twse.com.tw/mops/web"
OUTPUT_DIR = PROJECT_ROOT / "data" / "stocks" / "financials"
STOCK_DIR = PROJECT_ROOT / "data" / "stocks"
DEFAULT_SYMBOLS = ("2330", "2317", "6488", "0050")
ENDPOINTS = {
    "balance": "ajax_t164sb03",
    "income": "ajax_t164sb04",
    "cash_flow": "ajax_t164sb05",
}


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(); self.cell: list[str] | None = None; self.row: list[str] = []; self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr": self.row = []
        if tag in {"td", "th"}: self.cell = []

    def handle_data(self, data: str) -> None:
        if self.cell is not None: self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split())); self.cell = None
        if tag == "tr" and self.row: self.rows.append(self.row)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update official multi-quarter stock financials")
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--quarters", type=int, default=12)
    parser.add_argument("--delay", type=float, default=0.6)
    return parser.parse_args()


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()


def number(value: Any) -> int | float | None:
    text = str(value or "").replace(",", "").strip()
    if text in {"", "--", "-"}: return None
    if text.startswith("(") and text.endswith(")"): text=f"-{text[1:-1]}"
    try:
        result = round(float(text), 6)
        return int(result) if result.is_integer() else result
    except ValueError: return None


def quarter_end(year: int, quarter: int) -> str:
    return f"{year}-{quarter * 3:02d}-{31 if quarter in {1, 4} else 30:02d}"


def conservative_available_date(year: int, quarter: int) -> str:
    # Statutory filing deadlines are conservative availability bounds. Exact
    # early filing timestamps are not exposed by these statement endpoints.
    return {1: f"{year}-05-15", 2: f"{year}-08-14", 3: f"{year}-11-14", 4: f"{year + 1}-03-31"}[quarter]


def recent_quarters(count: int) -> list[tuple[int, int]]:
    today = date.today(); latest = (today.year, 4)
    for year, quarter in ((today.year, 3), (today.year, 2), (today.year, 1), (today.year - 1, 4)):
        if date.fromisoformat(conservative_available_date(year, quarter)) <= today:
            latest = (year, quarter); break
    serial = latest[0] * 4 + latest[1] - 1
    return [((value // 4), value % 4 + 1) for value in range(serial - count + 1, serial + 1)]


class MopsClient:
    def __init__(self, delay: float) -> None:
        self.delay = max(0, delay); self.last_request = 0.0; self.session = requests.Session()
        self.session.headers.update({"User-Agent": "taiwan-stock-analysis-platform/1.0", "Referer": f"{MOPS}/t164sb04"})

    def rows(self, endpoint: str, symbol: str, year: int, quarter: int) -> list[list[str]]:
        payload = {"encodeURIComponent": "1", "step": "1", "firstin": "1", "off": "1", "queryName": "co_id", "TYPEK": "all", "isnew": "false", "co_id": symbol, "year": str(year - 1911), "season": f"{quarter:02d}"}
        error: Exception | None = None
        for attempt in range(3):
            wait = self.delay - (time.monotonic() - self.last_request)
            if wait > 0: time.sleep(wait)
            try:
                response = self.session.post(f"{MOPS}/{endpoint}", data=payload, timeout=45); self.last_request = time.monotonic(); response.raise_for_status()
                if "FOR SECURITY REASONS" in response.text: raise ValueError("MOPS request was rate limited")
                if len(response.text) < 2000: raise ValueError("MOPS returned no statement")
                parser = TableParser(); parser.feed(response.text)
                if len(parser.rows) < 5: raise ValueError("MOPS statement table is empty")
                return parser.rows
            except (requests.RequestException, ValueError) as exc:
                error = exc
                if attempt < 2: time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"{symbol} {year}Q{quarter} {endpoint}: {error}")


def find(rows: list[list[str]], aliases: tuple[str, ...]) -> int | float | None:
    for alias in aliases:
        candidates = [row for row in rows if row and row[0].strip() == alias]
        for row in reversed(candidates):
            if len(row) > 1 and number(row[1]) is not None: return number(row[1])
    return None


ALIASES = {
    "revenue": ("營業收入合計", "營業收入"),
    "gross_profit": ("營業毛利（毛損）淨額", "營業毛利（毛損）", "營業毛利"),
    "operating_income": ("營業利益（損失）", "營業利益"),
    "net_income": ("本期淨利（淨損）", "本期淨利"),
    "eps": ("基本每股盈餘",),
    "current_assets": ("流動資產合計", "流動資產"),
    "total_assets": ("資產總計", "資產總額"),
    "current_liabilities": ("流動負債合計", "流動負債"),
    "total_liabilities": ("負債總計", "負債總額"),
    "total_equity": ("權益總計", "權益總額"),
    "operating_cash_flow_cumulative": ("營業活動之淨現金流入（流出）", "營業活動之淨現金流量"),
    "investing_cash_flow_cumulative": ("投資活動之淨現金流入（流出）", "投資活動之淨現金流量"),
    "financing_cash_flow_cumulative": ("籌資活動之淨現金流入（流出）", "籌資活動之淨現金流量"),
    "capital_expenditure_cumulative_raw": ("取得不動產、廠房及設備", "購置不動產、廠房及設備"),
}


def ratio(numerator: int | float | None, denominator: int | float | None) -> float | None:
    return round(numerator / denominator * 100, 6) if numerator is not None and denominator not in {None, 0} else None


class FinancialParserSchemaError(ValueError):
    def __init__(self, symbol: str, year: int, quarter: int, missing_fields: list[str]):
        self.missing_fields = missing_fields
        super().__init__(f"{symbol} {year}Q{quarter} missing required fields: {', '.join(missing_fields)}")


class FinancialDependencyMissing(ValueError):
    def __init__(self, symbol: str, year: int, quarter: int, dependencies: list[str]):
        self.dependencies = dependencies
        super().__init__(f"{symbol} {year}Q{quarter} missing dependencies: {', '.join(dependencies)}")


def parse_quarter_statements(symbol: str, year: int, quarter: int,
                             statements: dict[str, list[list[str]]]) -> tuple[dict[str, Any], list[str]]:
    """Parse one quarter without discarding partial values needed by later quarters."""
    values = {
        key: find(
            statements["income" if key in {"revenue", "gross_profit", "operating_income", "net_income", "eps"}
                       else "balance" if key in {"current_assets", "total_assets", "current_liabilities", "total_liabilities", "total_equity"}
                       else "cash_flow"],
            aliases,
        )
        for key, aliases in ALIASES.items()
    }
    required = ("revenue", "gross_profit", "operating_income", "net_income", "eps", "total_assets", "total_liabilities")
    missing = [key for key in required if values[key] is None]
    record = {
        "fiscal_year": year, "quarter": quarter, "period_end": quarter_end(year, quarter),
        # These statement endpoints do not expose the actual filing time.
        # Keep PIT availability unknown rather than substituting period-end
        # or a statutory deadline.
        "published_date": None, "available_date": None, "available_at": None,
        "source_date": quarter_end(year, quarter),
        "source": "MOPS consolidated IFRS statements",
        "unit": {"monetary": "thousand_TWD", "eps": "TWD", "ratio": "percent"},
        "income_period_basis": "year_to_date" if quarter == 4 else "standalone_quarter",
        **values,
        "equity": values["total_equity"],
        "gross_margin": ratio(values["gross_profit"], values["revenue"]),
        "operating_margin": ratio(values["operating_income"], values["revenue"]),
        "net_margin": ratio(values["net_income"], values["revenue"]),
        "debt_ratio": ratio(values["total_liabilities"], values["total_assets"]),
        "current_ratio": ratio(values["current_assets"], values["current_liabilities"]),
    }
    return record, missing


def derive_quarter(symbol: str, record: dict[str, Any],
                   context: dict[tuple[int, int], dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """Derive one canonical quarter using only explicit preceding-quarter context."""
    result = dict(record)
    year, quarter = int(result["fiscal_year"]), int(result["quarter"])
    missing: list[str] = []

    if quarter == 4 and result.get("income_period_basis") != "standalone_quarter":
        earlier = [context.get((year, prior)) for prior in (1, 2, 3)]
        for prior, dependency in zip((1, 2, 3), earlier):
            if dependency is None:
                missing.append(f"{year}Q{prior}:income")
        for field in ("revenue", "gross_profit", "operating_income", "net_income", "eps"):
            if not isinstance(result.get(field), (int, float)) or any(
                dependency is None or not isinstance(dependency.get(field), (int, float)) for dependency in earlier
            ):
                missing.append(f"{year}Q1-Q3:{field}")
        if missing:
            raise FinancialDependencyMissing(symbol, year, quarter, sorted(set(missing)))
        for field in ("revenue", "gross_profit", "operating_income", "net_income", "eps"):
            result[field] = round(result[field] - sum(dependency[field] for dependency in earlier if dependency), 6)
        result["income_period_basis"] = "standalone_quarter"

    prior_period = (year, quarter - 1) if quarter > 1 else (year - 1, 4)
    prior = context.get(prior_period)
    for cumulative, standalone in (
        ("operating_cash_flow_cumulative", "operating_cash_flow"),
        ("investing_cash_flow_cumulative", "investing_cash_flow"),
        ("financing_cash_flow_cumulative", "financing_cash_flow"),
    ):
        current = result.get(cumulative)
        if current is None:
            result[standalone] = None
        elif quarter == 1:
            result[standalone] = current
        elif prior is None or not isinstance(prior.get(cumulative), (int, float)):
            missing.append(f"{prior_period[0]}Q{prior_period[1]}:{cumulative}")
            result[standalone] = None
        else:
            result[standalone] = current - prior[cumulative]

    current_capex = result.get("capital_expenditure_cumulative_raw")
    if current_capex is None:
        result["capital_expenditure"] = None
    elif quarter == 1:
        result["capital_expenditure"] = abs(current_capex)
    elif prior is None or not isinstance(prior.get("capital_expenditure_cumulative_raw"), (int, float)):
        missing.append(f"{prior_period[0]}Q{prior_period[1]}:capital_expenditure_cumulative_raw")
        result["capital_expenditure"] = None
    else:
        result["capital_expenditure"] = abs(current_capex - prior["capital_expenditure_cumulative_raw"])

    if missing:
        raise FinancialDependencyMissing(symbol, year, quarter, sorted(set(missing)))

    result["free_cash_flow"] = (
        result["operating_cash_flow"] - result["capital_expenditure"]
        if result.get("operating_cash_flow") is not None and result.get("capital_expenditure") is not None else None
    )
    result["gross_margin"] = ratio(result.get("gross_profit"), result.get("revenue"))
    result["operating_margin"] = ratio(result.get("operating_income"), result.get("revenue"))
    result["net_margin"] = ratio(result.get("net_income"), result.get("revenue"))
    equity = result.get("equity", result.get("total_equity")); result["equity"] = equity
    previous_equity = prior.get("equity", prior.get("total_equity")) if prior else None
    average_equity = (equity + previous_equity) / 2 if equity is not None and previous_equity is not None else None
    result["roe"] = ratio(result["net_income"] * 4 if result.get("net_income") is not None else None, average_equity)
    result["roa"] = ratio(result["net_income"] * 4 if result.get("net_income") is not None else None, result.get("total_assets"))
    if prior is None or previous_equity is None:
        result["quality_flags"] = sorted(set([*(result.get("quality_flags") or []), "previous_equity_unavailable"]))
    return result, missing


def fetch_symbol(client: MopsClient, symbol: str, quarters: list[tuple[int, int]], cached: list[dict[str, Any]] | None = None,
                 component_loader: Callable[[MopsClient,str,int,int,str,str],list[list[str]]] | None = None) -> list[dict[str, Any]]:
    cached_by_period = {(row.get("fiscal_year"), row.get("quarter")): row for row in (cached or [])}
    records: list[dict[str, Any]] = []
    for year, quarter in quarters:
        if (year, quarter) in cached_by_period:
            records.append(dict(cached_by_period[(year, quarter)])); LOGGER.info("Reused %s %sQ%s", symbol, year, quarter); continue
        statements = {
            name:(component_loader(client,symbol,year,quarter,name,endpoint) if component_loader else client.rows(endpoint,symbol,year,quarter))
            for name,endpoint in ENDPOINTS.items()
        }
        record, missing = parse_quarter_statements(symbol, year, quarter, statements)
        if missing: raise FinancialParserSchemaError(symbol, year, quarter, missing)
        records.append(record)
        LOGGER.info("Fetched %s %sQ%s", symbol, year, quarter)

    # MOPS displays standalone income for Q1-Q3, but the annual cumulative
    # figure for Q4. Convert Q4 to a standalone quarter exactly once.
    for record in records:
        if record["quarter"] != 4:
            record["income_period_basis"] = "standalone_quarter"; continue
        if record.get("income_period_basis") == "standalone_quarter": continue
        earlier = [row for row in records if row["fiscal_year"] == record["fiscal_year"] and row["quarter"] in {1,2,3}]
        if len(earlier) != 3: raise ValueError(f"{symbol} {record['fiscal_year']}Q4 lacks Q1-Q3 income basis")
        for field in ("revenue","gross_profit","operating_income","net_income","eps"):
            if not isinstance(record.get(field),(int,float)) or not all(isinstance(row.get(field),(int,float)) for row in earlier): raise ValueError(f"{symbol} {record['fiscal_year']}Q4 cannot derive {field}")
            record[field] = round(record[field] - sum(row[field] for row in earlier),6)
        record["income_period_basis"] = "standalone_quarter"

    previous_equity: int | float | None = None
    previous_cumulative: dict[str, int | float] = {}
    for record in records:
        if record["quarter"] == 1: previous_cumulative = {}
        for cumulative, standalone in (("operating_cash_flow_cumulative", "operating_cash_flow"), ("investing_cash_flow_cumulative", "investing_cash_flow"), ("financing_cash_flow_cumulative", "financing_cash_flow")):
            current = record.get(cumulative); prior = previous_cumulative.get(cumulative, 0)
            record[standalone] = current - prior if current is not None else None
            if current is not None: previous_cumulative[cumulative] = current
        current_capex = record.get("capital_expenditure_cumulative_raw")
        prior_capex = previous_cumulative.get("capital_expenditure_cumulative_raw", 0)
        record["capital_expenditure"] = abs(current_capex - prior_capex) if current_capex is not None else None
        if current_capex is not None: previous_cumulative["capital_expenditure_cumulative_raw"] = current_capex
        record["free_cash_flow"] = record["operating_cash_flow"] - record["capital_expenditure"] if record.get("operating_cash_flow") is not None and record.get("capital_expenditure") is not None else None
        record["gross_margin"] = ratio(record.get("gross_profit"), record.get("revenue"))
        record["operating_margin"] = ratio(record.get("operating_income"), record.get("revenue"))
        record["net_margin"] = ratio(record.get("net_income"), record.get("revenue"))
        equity = record.get("equity",record.get("total_equity"));record["equity"]=equity
        average_equity = (equity + previous_equity) / 2 if equity is not None and previous_equity is not None else equity
        record["roe"] = ratio(record["net_income"] * 4 if record.get("net_income") is not None else None, average_equity)
        record["roa"] = ratio(record["net_income"] * 4 if record.get("net_income") is not None else None, record.get("total_assets"))
        previous_equity = equity
    return records


def main() -> int:
    # Fetch four leading quarters. This guarantees both a previous cumulative
    # cash-flow basis and Q1-Q3 income figures even when the requested window
    # happens to begin at Q4.
    options = arguments(); client = MopsClient(options.delay); quarters = recent_quarters(options.quarters + 4); failures = 0
    for symbol in options.symbols:
        stock_path = STOCK_DIR / f"{symbol}.json"
        if stock_path.exists():
            profile = json.loads(stock_path.read_text(encoding="utf-8")).get("data", {}).get("profile", {})
            if profile.get("instrument_type") != "company":
                LOGGER.info("Skipped %s: ETF/non-company", symbol); continue
        try:
            output_path = OUTPUT_DIR / f"{symbol}.json"
            cached = json.loads(output_path.read_text(encoding="utf-8"))["data"]["quarters"] if output_path.exists() else []
            records = fetch_symbol(client, symbol, quarters, cached)[-options.quarters:]
            if len(records) < 8: raise ValueError(f"only {len(records)} complete quarters")
            if records != sorted(records, key=lambda row: row["period_end"]): raise ValueError("quarter order is invalid")
            payload = {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "provider": "MOPS", "dataset": "stock_financial_history", "version": "1.0", "data": {"symbol": symbol, "statement_scope": "consolidated", "available_date_basis": "conservative statutory filing deadline; exact early publication date unavailable", "quarters": records}}
            atomic_json(output_path, payload)
            LOGGER.info("Wrote %s (%s quarters)", symbol, len(records))
        except Exception as exc:
            if output_path.exists(): LOGGER.error("Financial history failed for %s; existing valid cache preserved: %s", symbol, exc)
            else: failures += 1; LOGGER.error("Financial history failed for %s and no prior cache exists: %s", symbol, exc)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    raise SystemExit(main())
