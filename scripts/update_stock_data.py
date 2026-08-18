"""Build static, official-source stock analysis caches for GitHub Pages.

Usage:
    python scripts/update_stock_data.py
    python scripts/update_stock_data.py --symbols 2330 2317 0050 6488

The browser never calls upstream sites. It searches data/stocks/index.json and
loads only pre-built symbol caches. Additional listed/OTC symbols use the same
pipeline and do not require page changes.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import requests

from config import PROJECT_ROOT


LOGGER = logging.getLogger("stock_data")
OUTPUT_DIR = PROJECT_ROOT / "data" / "stocks"
CHIPS_DAILY = PROJECT_ROOT / "data" / "history" / "chips_daily.csv"
DEFAULT_SYMBOLS = ("2330", "2317", "0050", "6488")
TIMEOUT = 45

TWSE = "https://openapi.twse.com.tw/v1"
TPEX = "https://www.tpex.org.tw/openapi/v1"
TWSE_DAILY = "https://www.twse.com.tw/rwd/zh"
TPEX_DAILY = "https://www.tpex.org.tw/www/zh-tw"

URLS = {
    "twse_profile": f"{TWSE}/opendata/t187ap03_L",
    "twse_quote": f"{TWSE}/exchangeReport/STOCK_DAY_ALL",
    "twse_valuation": f"{TWSE}/exchangeReport/BWIBBU_ALL",
    "twse_revenue": f"{TWSE}/opendata/t187ap05_L",
    "twse_eps": f"{TWSE}/opendata/t187ap14_L",
    "twse_profitability": f"{TWSE}/opendata/t187ap17_L",
    "twse_income": f"{TWSE}/opendata/t187ap06_L_ci",
    "twse_balance": f"{TWSE}/opendata/t187ap07_L_ci",
    "tpex_profile": f"{TPEX}/mopsfin_t187ap03_O",
    "tpex_quote": f"{TPEX}/tpex_mainboard_quotes",
    "tpex_valuation": f"{TPEX}/tpex_mainboard_peratio_analysis",
    "tpex_revenue": f"{TPEX}/mopsfin_t187ap05_O",
    "tpex_eps": f"{TPEX}/mopsfin_t187ap14_O",
    "tpex_income": f"{TPEX}/mopsfin_t187ap06_O_ci",
    "tpex_balance": f"{TPEX}/mopsfin_t187ap07_O_ci",
}

# Public first-version health rules. Every threshold is deterministic and kept
# here rather than scattered through the UI. Missing/not-applicable values are
# reported as unavailable and never converted to zero.
HEALTH_RULES = {
    "positive_growth": {"positive_min": 0.000001, "warning_max": -0.000001},
    "roe": {"positive_min": 15.0, "warning_max": 5.0},
    "profit_margin": {"positive_min": 10.0, "warning_max": 0.0},
    "pe": {"positive_max": 20.0, "warning_min": 40.0},
    "pb": {"positive_max": 3.0, "warning_min": 6.0},
    "yield": {"positive_min": 4.0, "warning_max": 2.0},
    "debt_ratio": {"positive_max": 50.0, "warning_min": 70.0},
    "current_ratio": {"positive_min": 200.0, "warning_max": 100.0},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update static stock analysis caches")
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--chips-days", type=int, default=20)
    parser.add_argument("--delay", type=float, default=0.15)
    return parser.parse_args()


class OfficialClient:
    def __init__(self, delay: float) -> None:
        self.delay = max(delay, 0)
        self.last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "taiwan-stock-analysis-platform/1.0"})

    def json(self, url: str, *, params: Mapping[str, str] | None = None) -> Any:
        error: Exception | None = None
        for attempt in range(3):
            wait = self.delay - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            try:
                response = self.session.get(url, params=params, timeout=TIMEOUT)
                self.last_request = time.monotonic()
                response.raise_for_status()
                return response.json()
            except requests.exceptions.SSLError:
                # TPEx's official certificate lacks an extension required by
                # Python 3.13 on Windows; retry only this known TLS condition.
                response = self.session.get(url, params=params, timeout=TIMEOUT, verify=False)
                self.last_request = time.monotonic()
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                error = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"official source failed: {url}: {error}")


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def number(value: Any) -> int | float | None:
    text = str(value if value is not None else "").replace(",", "").strip()
    if text in {"", "--", "---", "N/A", "-"}:
        return None
    try:
        parsed = round(float(text), 6)
        return int(parsed) if parsed.is_integer() else parsed
    except ValueError:
        return None


def integer(value: Any) -> int | None:
    parsed = number(value)
    return parsed if isinstance(parsed, int) else None


def roc_date(value: Any) -> str | None:
    text = str(value or "").replace("/", "").strip()
    if len(text) == 7 and text.isdigit():
        return date(int(text[:3]) + 1911, int(text[3:5]), int(text[5:7])).isoformat()
    if len(text) == 8 and text.isdigit():
        return date(int(text[:4]), int(text[4:6]), int(text[6:8])).isoformat()
    return None


def roc_month(value: Any) -> str | None:
    text = str(value or "").strip()
    return f"{int(text[:3]) + 1911:04d}-{text[3:5]}" if len(text) == 5 and text.isdigit() else None


def index_rows(rows: Any, key: str) -> dict[str, Mapping[str, Any]]:
    return {str(row.get(key, "")).strip(): row for row in rows if isinstance(row, dict) and row.get(key)} if isinstance(rows, list) else {}


def safe_table(client: OfficialClient, key: str) -> list[Mapping[str, Any]]:
    try:
        value = client.json(URLS[key])
        if not isinstance(value, list):
            raise ValueError("response is not a list")
        return [row for row in value if isinstance(row, dict)]
    except Exception as exc:
        LOGGER.error("%s unavailable: %s", key, exc)
        return []


def latest_trading_dates(count: int) -> list[str]:
    with CHIPS_DAILY.open("r", encoding="utf-8-sig", newline="") as handle:
        dates = [row["trade_date"] for row in csv.DictReader(handle)]
    if len(dates) < count:
        raise ValueError(f"chips calendar has only {len(dates)} dates")
    return dates[-count:]


def find_table(payload: Any, title_phrase: str) -> Mapping[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("response is not an object")
    for table in payload.get("tables", []):
        if isinstance(table, dict) and title_phrase in str(table.get("title", "")):
            return table
    raise ValueError(f"table not found: {title_phrase}")


def row_map(table: Mapping[str, Any], code_index: int = 0) -> dict[str, list[Any]]:
    return {str(row[code_index]).strip(): row for row in table.get("data", []) if isinstance(row, list) and len(row) > code_index}


def fetch_chips_history(client: OfficialClient, dates: Iterable[str], symbols: set[str], markets: Mapping[str, str]) -> dict[str, list[dict[str, Any]]]:
    output = {symbol: [] for symbol in symbols}
    listed = {symbol for symbol in symbols if markets.get(symbol) == "TWSE"}
    otc = {symbol for symbol in symbols if markets.get(symbol) == "TPEx"}
    for day in dates:
        compact = day.replace("-", "")
        slash = day.replace("-", "/")
        daily: dict[str, dict[str, Any]] = {}
        if listed:
            try:
                inst = client.json(f"{TWSE_DAILY}/fund/T86", params={"date": compact, "selectType": "ALLBUT0999", "response": "json"})
                fields = inst.get("fields", [])
                data = {str(row[0]).strip(): row for row in inst.get("data", []) if isinstance(row, list)}
                indexes = {name: fields.index(name) for name in ("外陸資買賣超股數(不含外資自營商)", "投信買賣超股數", "自營商買賣超股數", "三大法人買賣超股數")}
                margin = client.json(f"{TWSE_DAILY}/marginTrading/MI_MARGN", params={"date": compact, "selectType": "ALL", "response": "json"})
                margins = row_map(find_table(margin, "融資融券彙總"))
                for symbol in listed:
                    if symbol in data and symbol in margins:
                        row, credit = data[symbol], margins[symbol]
                        daily[symbol] = {"trade_date": day, "foreign_net": integer(row[indexes["外陸資買賣超股數(不含外資自營商)"]]), "investment_trust_net": integer(row[indexes["投信買賣超股數"]]), "dealer_net": integer(row[indexes["自營商買賣超股數"]]), "institutional_total": integer(row[indexes["三大法人買賣超股數"]]), "margin_balance": integer(credit[6]), "short_balance": integer(credit[12])}
            except Exception as exc:
                LOGGER.warning("TWSE chips %s unavailable: %s", day, exc)
        if otc:
            try:
                inst = client.json(f"{TPEX_DAILY}/insti/dailyTrade", params={"date": slash, "type": "Daily", "response": "json"})
                data = row_map(find_table(inst, "三大法人買賣明細"))
                margin = client.json(f"{TPEX_DAILY}/margin/balance", params={"date": slash, "response": "json"})
                margins = row_map(find_table(margin, "融資融券餘額"))
                for symbol in otc:
                    if symbol in data and symbol in margins:
                        row, credit = data[symbol], margins[symbol]
                        # TPEx columns: foreign total 2:5, trust 11:14, dealer total 20:23, grand total 23.
                        daily[symbol] = {"trade_date": day, "foreign_net": integer(row[4]), "investment_trust_net": integer(row[13]), "dealer_net": integer(row[22]), "institutional_total": integer(row[23]), "margin_balance": integer(credit[6]), "short_balance": integer(credit[14])}
            except Exception as exc:
                LOGGER.warning("TPEx chips %s unavailable: %s", day, exc)
        for symbol, row in daily.items():
            if all(row[field] is not None for field in row if field != "trade_date"):
                output[symbol].append(row)
    return output


def streak(rows: list[Mapping[str, Any]], field: str) -> dict[str, Any]:
    if not rows:
        return {"direction": None, "days": 0}
    sign = 1 if rows[-1][field] > 0 else -1 if rows[-1][field] < 0 else 0
    days = 0
    for row in reversed(rows):
        if (1 if row[field] > 0 else -1 if row[field] < 0 else 0) != sign:
            break
        days += 1
    return {"direction": "buy" if sign > 0 else "sell" if sign < 0 else "neutral", "days": days}


def chips_summary(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "available_days": len(rows),
        "foreign_streak": streak(rows, "foreign_net"),
        "investment_trust_streak": streak(rows, "investment_trust_net"),
        "five_day": {field: sum(row[field] for row in rows[-5:]) for field in ("foreign_net", "investment_trust_net", "dealer_net", "institutional_total")},
        "twenty_day": {field: sum(row[field] for row in rows[-20:]) for field in ("foreign_net", "investment_trust_net", "dealer_net", "institutional_total")},
    }


def metric(value: Any, data_date: str | None, unit: str, status: str | None = None, note: str | None = None) -> dict[str, Any]:
    return {"value": value, "unit": unit, "data_date": data_date, "status": status, "note": note}


def classify(value: int | float | None, rule_name: str) -> str:
    if value is None:
        return "unavailable"
    rule = HEALTH_RULES[rule_name]
    if "positive_min" in rule and value >= rule["positive_min"]:
        return "positive"
    if "positive_max" in rule and 0 < value <= rule["positive_max"]:
        return "positive"
    if "warning_max" in rule and value < rule["warning_max"]:
        return "warning"
    if "warning_min" in rule and value >= rule["warning_min"]:
        return "warning"
    return "neutral"


def create_health(fundamentals: Mapping[str, Any], valuation: Mapping[str, Any], is_company: bool) -> dict[str, Any]:
    if not is_company:
        return {"applicable": False, "reason": "ETF／非一般公司不適用公司財報健檢", "rules": HEALTH_RULES, "categories": {}}
    values = {**fundamentals, **valuation}
    specs = {
        "growth": (("revenue_yoy", "營收 YoY", "positive_growth"), ("eps_growth", "EPS 成長", "positive_growth"), ("roe", "ROE", "roe")),
        "profitability": (("gross_margin", "毛利率", "profit_margin"), ("operating_margin", "營業利益率", "profit_margin"), ("net_margin", "稅後淨利率", "profit_margin"), ("roe", "ROE", "roe")),
        "valuation": (("pe", "PE", "pe"), ("pb", "PB", "pb"), ("dividend_yield", "殖利率", "yield")),
        "financial_risk": (("debt_ratio", "負債比", "debt_ratio"), ("current_ratio", "流動比率", "current_ratio"), ("operating_cash_flow", "營業現金流", "positive_growth"), ("free_cash_flow", "自由現金流", "positive_growth")),
    }
    categories: dict[str, list[dict[str, Any]]] = {}
    for category, items in specs.items():
        categories[category] = []
        for key, label, rule in items:
            source = values.get(key, {})
            categories[category].append({"key": key, "label": label, **source, "status": classify(source.get("value"), rule)})
    return {"applicable": True, "rules": HEALTH_RULES, "categories": categories}


def build_stock(symbol: str, market: str, tables: Mapping[str, Mapping[str, Mapping[str, Any]]], history: list[dict[str, Any]]) -> dict[str, Any]:
    prefix = "twse" if market == "TWSE" else "tpex"
    profile = tables[f"{prefix}_profile"].get(symbol, {})
    quote = tables[f"{prefix}_quote"].get(symbol, {})
    valuation_row = tables[f"{prefix}_valuation"].get(symbol, {})
    revenue = tables[f"{prefix}_revenue"].get(symbol, {})
    eps = tables[f"{prefix}_eps"].get(symbol, {})
    profitability = tables.get(f"{prefix}_profitability", {}).get(symbol, {})
    income = tables[f"{prefix}_income"].get(symbol, {})
    balance = tables[f"{prefix}_balance"].get(symbol, {})
    is_twse = market == "TWSE"
    get = lambda row, twse, tpex=None: row.get(twse if is_twse else (tpex or twse))
    name = get(profile, "公司簡稱", "CompanyAbbreviation") or get(quote, "Name", "CompanyName") or symbol
    instrument_type = "ETF" if symbol.startswith("00") and not profile else "company"
    industry = get(revenue, "產業別") if instrument_type == "company" else "ETF"
    quote_date = roc_date(get(quote, "Date"))
    close = number(get(quote, "ClosingPrice", "Close")); change_value = number(get(quote, "Change"))
    previous = close - change_value if close is not None and change_value is not None else None
    change_percent = round(change_value / previous * 100, 4) if previous and previous > 0 else None
    source_report_date = roc_date(get(eps, "出表日期", "Date")); year = get(eps, "年度", "Year"); quarter = get(eps, "季別")
    period = f"{int(year) + 1911} Q{quarter}" if str(year).isdigit() and str(quarter).isdigit() else None
    quarter_ends = {"1": "03-31", "2": "06-30", "3": "09-30", "4": "12-31"}
    report_date = f"{int(year) + 1911}-{quarter_ends[str(quarter)]}" if str(year).isdigit() and str(quarter) in quarter_ends else None
    net_income = number(get(income, "本期淨利（淨損）")); equity = number(get(balance, "權益總計")); assets = number(get(balance, "資產總計")); liabilities = number(get(balance, "負債總計")); current_assets = number(get(balance, "流動資產")); current_liabilities = number(get(balance, "流動負債"))
    annualizer = 4 / int(quarter) if str(quarter).isdigit() and int(quarter) > 0 else None
    roe = round(net_income / equity * annualizer * 100, 4) if net_income is not None and equity and annualizer else None
    income_revenue = number(get(income, "營業收入"))
    def reported_or_calculated(reported_key: str, income_key: str) -> int | float | None:
        reported = number(get(profitability, reported_key))
        amount = number(get(income, income_key))
        return reported if reported is not None else round(amount / income_revenue * 100, 4) if amount is not None and income_revenue else None
    fundamentals = {
        "report_period": period, "report_date": report_date, "source_updated_at": source_report_date,
        "eps": metric(number(get(eps, "基本每股盈餘(元)", "基本每股盈餘")), report_date, "TWD"),
        "eps_growth": metric(None, report_date, "%", note="官方最新季度彙總未提供可可靠對齊的去年同期 EPS"),
        "roe": metric(roe, report_date, "%", note="累計稅後淨利年化／期末權益"),
        "revenue": metric(integer(get(revenue, "營業收入-當月營收")), roc_month(get(revenue, "資料年月")), "thousand_TWD"),
        "revenue_yoy": metric(number(get(revenue, "營業收入-去年同月增減(%)")), roc_month(get(revenue, "資料年月")), "%"),
        "revenue_mom": metric(number(get(revenue, "營業收入-上月比較增減(%)")), roc_month(get(revenue, "資料年月")), "%"),
        "gross_margin": metric(reported_or_calculated("毛利率(%)(營業毛利)/(營業收入)", "營業毛利（毛損）淨額"), report_date, "%", note="TPEx 標的由同季損益表計算" if not profitability else None),
        "operating_margin": metric(reported_or_calculated("營業利益率(%)(營業利益)/(營業收入)", "營業利益（損失）"), report_date, "%", note="TPEx 標的由同季損益表計算" if not profitability else None),
        "net_margin": metric(reported_or_calculated("稅後純益率(%)(稅後純益)/(營業收入)", "本期淨利（淨損）"), report_date, "%", note="TPEx 標的由同季損益表計算" if not profitability else None),
        "book_value_per_share": metric(number(get(balance, "每股參考淨值")), report_date, "TWD"),
        "debt_ratio": metric(round(liabilities / assets * 100, 4) if liabilities is not None and assets else None, report_date, "%"),
        "current_ratio": metric(round(current_assets / current_liabilities * 100, 4) if current_assets is not None and current_liabilities else None, report_date, "%"),
        "operating_cash_flow": metric(None, report_date, "thousand_TWD", note="目前官方 OpenAPI 未提供一致的現金流量彙總"),
        "free_cash_flow": metric(None, report_date, "thousand_TWD", note="缺少可一致對齊的資本支出欄位"),
    } if instrument_type == "company" else {"report_period": None, "report_date": None}
    valuation_date = roc_date(get(valuation_row, "Date"))
    valuation = {
        "pe": metric(number(get(valuation_row, "PEratio", "PriceEarningRatio")), valuation_date, "ratio"),
        "pb": metric(number(get(valuation_row, "PBratio", "PriceBookRatio")), valuation_date, "ratio"),
        "dividend_yield": metric(number(get(valuation_row, "DividendYield", "YieldRatio")), valuation_date, "%"),
    }
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "provider": "TWSE/TPEx/MOPS", "dataset": "stock_analysis", "version": "1.0",
        "data": {
            "profile": {"symbol": symbol, "name": str(name).strip(), "market": market, "industry": industry or "資料不足", "instrument_type": instrument_type},
            "quote": {"trade_date": quote_date, "close": close, "change": change_value, "change_percent": change_percent, "volume": integer(get(quote, "TradeVolume", "TradingShares")), "unit": {"price": "TWD", "volume": "shares"}},
            "valuation": valuation, "fundamentals": fundamentals,
            "chips": {"trade_date": history[-1]["trade_date"] if history else None, "unit": {"institutional": "shares", "margin": "trading_units"}, "history": history, "summary": chips_summary(history)},
        },
        "sources": {key: URLS[key] for key in URLS if key.startswith(prefix)},
    }
    payload["data"]["health"] = create_health(fundamentals, valuation, instrument_type == "company")
    return payload


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    args = parse_args(); symbols = {str(item).strip().upper() for item in args.symbols if str(item).strip()}
    client = OfficialClient(args.delay)
    raw = {key: safe_table(client, key) for key in URLS}
    keys = {"twse_profile": "公司代號", "twse_quote": "Code", "twse_valuation": "Code", "twse_revenue": "公司代號", "twse_eps": "公司代號", "twse_profitability": "公司代號", "twse_income": "公司代號", "twse_balance": "公司代號", "tpex_profile": "SecuritiesCompanyCode", "tpex_quote": "SecuritiesCompanyCode", "tpex_valuation": "SecuritiesCompanyCode", "tpex_revenue": "公司代號", "tpex_eps": "SecuritiesCompanyCode", "tpex_income": "SecuritiesCompanyCode", "tpex_balance": "SecuritiesCompanyCode"}
    tables = {name: index_rows(rows, keys[name]) for name, rows in raw.items()}
    markets: dict[str, str] = {}
    stock_index: list[dict[str, Any]] = []
    for market, table_name, code_key, name_key in (("TWSE", "twse_quote", "Code", "Name"), ("TPEx", "tpex_quote", "SecuritiesCompanyCode", "CompanyName")):
        for row in raw[table_name]:
            symbol = str(row.get(code_key, "")).strip()
            if not symbol:
                continue
            markets[symbol] = market
            profile = tables[f"{'twse' if market == 'TWSE' else 'tpex'}_profile"].get(symbol, {})
            revenue = tables[f"{'twse' if market == 'TWSE' else 'tpex'}_revenue"].get(symbol, {})
            is_company = bool(profile)
            stock_index.append({"symbol": symbol, "name": str(row.get(name_key, "")).strip(), "market": market, "industry": revenue.get("產業別") if is_company else "ETF／其他", "instrument_type": "company" if is_company else "ETF" if symbol.startswith("00") else "other", "cached": symbol in symbols or (OUTPUT_DIR / f"{symbol}.json").exists()})
    atomic_json(OUTPUT_DIR / "index.json", {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "version": "1.0", "stocks": sorted(stock_index, key=lambda row: row["symbol"])})
    unknown = symbols - set(markets)
    if unknown:
        LOGGER.error("Unknown symbols: %s", ", ".join(sorted(unknown)))
    selected = symbols & set(markets)
    histories = fetch_chips_history(client, latest_trading_dates(args.chips_days), selected, markets)
    success = 0
    for symbol in sorted(selected):
        try:
            if len(histories[symbol]) < args.chips_days:
                raise ValueError(f"only {len(histories[symbol])}/{args.chips_days} complete chips days")
            payload = build_stock(symbol, markets[symbol], tables, histories[symbol])
            atomic_json(OUTPUT_DIR / f"{symbol}.json", payload)
            LOGGER.info("Updated %s %s", symbol, payload["data"]["profile"]["name"])
            success += 1
        except Exception as exc:
            LOGGER.error("%s failed; existing cache preserved: %s", symbol, exc)
    LOGGER.info("Stock cache summary | requested=%d success=%d", len(symbols), success)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
