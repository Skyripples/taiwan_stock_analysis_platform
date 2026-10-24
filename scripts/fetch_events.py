from __future__ import annotations

import argparse
import html
import json
import re
import time as time_module
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_PATH = DATA_DIR / "event.json"
DEFAULT_TARGET_YEAR = 2026

TAIPEI_TZ = timezone(timedelta(hours=8))

TWSE_EXRIGHT_OPENAPI_URL = "https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL"
TWSE_DISPOSITION_URL = "https://www.twse.com.tw/announcement/punish?response=json"
TPEX_DISPOSITION_URL = "https://www.tpex.org.tw/www/zh-tw/bulletin/disposal?response=json"
YAHOO_DIVIDEND_URL = "https://tw.stock.yahoo.com/calendar/dividend"
YAHOO_EARNINGS_URL = "https://tw.stock.yahoo.com/calendar/earnings-call"
YAHOO_DELISTING_URL = "https://tw.stock.yahoo.com/calendar/delisting"
YAHOO_HOLDERS_MEETING_URL = "https://tw.stock.yahoo.com/calendar/holders-meeting"
USE_TWSE_DIVIDEND_API = False
USE_MOPS_CALENDAR = False

# Yahoo 行事曆頁面的可查詢範圍：往前 365 天、往後 180 天。
YAHOO_DAY_RANGE_BEFORE = 365
YAHOO_DAY_RANGE_AFTER = 180
YAHOO_SWEEP_STEP_DAYS = 14
YAHOO_REQUEST_DELAY_SECONDS = 0.2
YAHOO_MAX_RETRIES = 3

# 先放你目前確認可用的 MOPS 月曆 AJAX
# 之後可再補更多月份對應的 URL
MOPS_CALENDAR_AJAX_URLS = [
    "https://mopsov.twse.com.tw/mops/web/ajax_t108sb31new?parameters=32b138d25ee38c00fbf70ec5a53724972a4500829419c933492103358a9148bf8071084b1d15b7c2be8059c3409a33790215e2716103522dc12fcade661b00b9cb43e69e3adaae4bf31480e2c089bfa8",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/json,text/plain,*/*",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.verify = False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


@dataclass
class EventItem:
    stock_id: str
    stock_name: str
    type: str
    title: str
    date: str
    source: str
    url: str
    time: str = ""
    ex_date: str = ""
    pay_date: str = ""
    cash_dividend: str = ""
    disposition_start_date: str = ""
    disposition_end_date: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch stock events into data/event.json")
    parser.add_argument("--year", type=int, default=DEFAULT_TARGET_YEAR)
    return parser.parse_args()


def clean_text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def fetch_text(url: str) -> str:
    resp = SESSION.get(url, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


# -------------------------
# 除權息：TWSE（保留，預設停用）
# -------------------------
def roc_to_iso(roc: str) -> Optional[str]:
    if not re.fullmatch(r"\d{7,8}", roc):
        return None
    y = int(roc[:-4]) + 1911
    m = int(roc[-4:-2])
    d = int(roc[-2:])
    try:
        return f"{y:04d}-{m:02d}-{d:02d}"
    except Exception:
        return None


def roc_slash_to_iso(value: Any) -> Optional[str]:
    """Convert a ROC date such as ``115/10/28`` to an ISO date."""
    match = re.fullmatch(r"\s*(\d{2,3})/(\d{1,2})/(\d{1,2})\s*", clean_text(value))
    if not match:
        return None
    try:
        return date(int(match.group(1)) + 1911, int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return None


def parse_disposition_period(value: Any) -> Optional[Tuple[str, str]]:
    """Extract the official ROC start/end dates from a disposition period."""
    dates = re.findall(r"\d{2,3}/\d{1,2}/\d{1,2}", clean_text(value))
    if len(dates) < 2:
        return None
    start_date = roc_slash_to_iso(dates[0])
    end_date = roc_slash_to_iso(dates[-1])
    if not start_date or not end_date or end_date < start_date:
        return None
    return start_date, end_date


def _disposition_event(
    *,
    stock_id: Any,
    stock_name: Any,
    period: Any,
    source: str,
    url: str,
    today: date,
) -> Optional[EventItem]:
    """Map one official disposition row, retaining only currently active stocks."""
    symbol = clean_text(stock_id)
    name = clean_text(stock_name)
    parsed_period = parse_disposition_period(period)
    if not re.fullmatch(r"\d{4}", symbol) or not name or not parsed_period:
        return None

    start_date, end_date = parsed_period
    if not (start_date <= today.isoformat() <= end_date):
        return None

    return EventItem(
        stock_id=symbol,
        stock_name=name,
        type="disposition",
        title=f"{symbol} {name}（處置中，預計出關 {end_date}）",
        date=start_date,
        source=source,
        url=url,
        disposition_start_date=start_date,
        disposition_end_date=end_date,
    )


def fetch_twse_disposition_events(today: Optional[date] = None) -> List[EventItem]:
    """Fetch currently active listed-company dispositions from TWSE."""
    response = SESSION.get(TWSE_DISPOSITION_URL, timeout=30)
    response.raise_for_status()
    payload = response.json()
    active_on = today or datetime.now(TAIPEI_TZ).date()
    events: List[EventItem] = []

    for row in payload.get("data", []):
        if not isinstance(row, list) or len(row) < 7:
            continue
        event = _disposition_event(
            stock_id=row[2],
            stock_name=row[3],
            period=row[6],
            source="TWSE",
            url=TWSE_DISPOSITION_URL,
            today=active_on,
        )
        if event:
            events.append(event)
    return events


def fetch_tpex_disposition_events(today: Optional[date] = None) -> List[EventItem]:
    """Fetch currently active OTC-company dispositions from TPEx."""
    response = SESSION.get(TPEX_DISPOSITION_URL, timeout=30)
    response.raise_for_status()
    payload = response.json()
    active_on = today or datetime.now(TAIPEI_TZ).date()
    events: List[EventItem] = []

    for table in payload.get("tables", []):
        if not isinstance(table, dict):
            continue
        for row in table.get("data", []):
            if not isinstance(row, list) or len(row) < 6:
                continue
            stock_name = clean_text(row[3]).split("(", 1)[0].strip()
            event = _disposition_event(
                stock_id=row[2],
                stock_name=stock_name,
                period=row[5],
                source="TPEx",
                url=TPEX_DISPOSITION_URL,
                today=active_on,
            )
            if event:
                events.append(event)
    return events


def fetch_dividend(year: int) -> List[EventItem]:
    resp = SESSION.get(TWSE_EXRIGHT_OPENAPI_URL, timeout=30)
    resp.raise_for_status()
    rows = resp.json()

    events: List[EventItem] = []

    for r in rows:
        stock_id = clean_text(r.get("Code"))
        stock_name = clean_text(r.get("Name"))
        date = roc_to_iso(clean_text(r.get("Date")))

        if not stock_id or not stock_name or not date:
            continue
        if not date.startswith(str(year)):
            continue

        events.append(
            EventItem(
                stock_id=stock_id,
                stock_name=stock_name,
                type="dividend",
                title=f"{stock_id} {stock_name}(除權息)",
                date=date,
                source="TWSE",
                url=TWSE_EXRIGHT_OPENAPI_URL,
            )
        )

    return events


# -------------------------
# MOPS 月曆：除權息 / 股東會
# -------------------------
def yyyymmdd_to_iso(date_text: str) -> Optional[str]:
    date_text = clean_text(date_text)
    if not re.fullmatch(r"\d{8}", date_text):
        return None
    try:
        dt = datetime.strptime(date_text, "%Y%m%d")
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%d")


def parse_mops(url: str) -> List[EventItem]:
    text = fetch_text(url)
    events: List[EventItem] = []
    entry_pattern = re.compile(
        r"COMP\.value='(?P<stock_id>[^']+)';document\.t108sb31new_fm2\.DATE1\.value='(?P<date>\d{8})';.*?\">(?P<link_text>.*?)</u>",
        re.S,
    )

    for m in entry_pattern.finditer(text):
        stock_id = clean_text(m.group("stock_id"))
        date = yyyymmdd_to_iso(m.group("date"))
        if not stock_id or not date:
            continue

        link_html = m.group("link_text")
        line = html.unescape(re.sub(r"<[^>]+>", "", link_html))
        line = line.replace("\xa0", " ")
        line = re.sub(r"\s+", " ", line).strip()

        row_match = re.match(
            rf"^{re.escape(stock_id)}\s+(.+?)[(（]([除股臨])[)）]$",
            line,
        )
        if not row_match:
            continue

        stock_name = row_match.group(1).strip()
        flag = row_match.group(2)

        if flag == "除":
            event_type = "dividend"
            label = "除權息"
        elif flag == "股":
            event_type = "shareholder_meeting"
            label = "股東會"
        else:
            event_type = "shareholder_meeting"
            label = "股東臨時會"

        events.append(
            EventItem(
                stock_id=stock_id,
                stock_name=stock_name,
                type=event_type,
                title=f"{stock_id} {stock_name}({label})",
                date=date,
                source="MOPS",
                url=url,
            )
        )

    return events


def fetch_mops(year: int) -> List[EventItem]:
    events: List[EventItem] = []

    for url in MOPS_CALENDAR_AJAX_URLS:
        try:
            month_events = parse_mops(url)
        except Exception as e:
            print(f"[warn] MOPS parse failed: {url} | {e}")
            continue

        for e in month_events:
            if e.date.startswith(str(year)):
                events.append(e)

    return events


# -------------------------
# Yahoo 法說會
# -------------------------
def parse_yahoo_datetime_parts(text: str) -> Tuple[Optional[str], str]:
    text = clean_text(text)
    if not text:
        return None, ""

    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    except ValueError:
        pass

    m = re.search(r"(?P<date>\d{4}-\d{2}-\d{2})(?:[ T](?P<time>\d{2}:\d{2})(?::\d{2})?)?", text)
    if m:
        return m.group("date"), clean_text(m.group("time"))

    formats = ["%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y-%m-%d %H:%M", "%Y-%m-%d"]
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M") if "%H:%M" in fmt else ""
        except ValueError:
            continue
    return None, ""


def normalize_yahoo_symbol(symbol_text: str) -> str:
    symbol_text = clean_text(symbol_text)
    return re.sub(r"\.(TW|TWO)$", "", symbol_text, flags=re.IGNORECASE)


def parse_yahoo_state(html: str) -> Optional[Dict[str, Any]]:
    m = re.search(r"root\.App\.main\s*=\s*(\{.*?\});\s*\n", html, re.S)
    if not m:
        m = re.search(r"root\.App\.main\s*=\s*(\{.*?\});\s*</script>", html, re.S)
    if not m:
        return None

    raw = m.group(1)
    normalized = raw
    normalized = re.sub(r":-Infinity([,}])", r":null\1", normalized)
    normalized = re.sub(r":Infinity([,}])", r":null\1", normalized)
    normalized = re.sub(r":NaN([,}])", r":null\1", normalized)
    normalized = re.sub(r":undefined([,}])", r":null\1", normalized)

    try:
        return json.loads(normalized)
    except json.JSONDecodeError:
        return None


def extract_yahoo_calendars(state: Dict[str, Any]) -> Dict[str, Any]:
    calendars = (
        state.get("context", {})
        .get("dispatcher", {})
        .get("stores", {})
        .get("CalendarsStore", {})
        .get("data", {})
        .get("calendars", {})
    )
    return calendars if isinstance(calendars, dict) else {}


def get_yahoo_query_range_for_year(year: int) -> Optional[Tuple[date, date]]:
    today_tpe = datetime.now(TAIPEI_TZ).date()
    available_start = today_tpe - timedelta(days=YAHOO_DAY_RANGE_BEFORE)
    available_end = today_tpe + timedelta(days=YAHOO_DAY_RANGE_AFTER)

    target_start = date(year, 1, 1)
    target_end = date(year, 12, 31)

    query_start = max(target_start, available_start)
    query_end = min(target_end, available_end)

    if query_start > query_end:
        return None
    return query_start, query_end


def build_yahoo_selected_dates(year: int) -> List[str]:
    query_range = get_yahoo_query_range_for_year(year)
    if not query_range:
        return []

    query_start, query_end = query_range
    selected_dates: List[str] = []

    d = query_start
    while d <= query_end:
        selected_dates.append(d.isoformat())
        d += timedelta(days=YAHOO_SWEEP_STEP_DAYS)

    end_text = query_end.isoformat()
    if selected_dates and selected_dates[-1] != end_text:
        selected_dates.append(end_text)

    return selected_dates


def fetch_yahoo_state_for_date(page_url: str, selected_date: str) -> Optional[Dict[str, Any]]:
    url = f"{page_url}?date={selected_date}"

    for attempt in range(1, YAHOO_MAX_RETRIES + 1):
        text = fetch_text(url)
        if "Request denied" in text:
            if attempt < YAHOO_MAX_RETRIES:
                time_module.sleep(float(attempt))
                continue
            return None

        state = parse_yahoo_state(text)
        if state:
            return state

        if attempt < YAHOO_MAX_RETRIES:
            time_module.sleep(0.5 * attempt)

    return None


def parse_positive_number(value: Any) -> Optional[float]:
    text = clean_text(value).replace(",", "")
    if not text:
        return None
    try:
        num = float(text)
    except ValueError:
        return None
    return num if num > 0 else None


def format_cash_dividend(cash_value: Any) -> str:
    cash_amount = parse_positive_number(cash_value)
    if cash_amount is None:
        return ""
    return f"{cash_amount:.2f}"


def build_dividend_payment_text(cash_value: Any, stock_value: Any) -> str:
    stock_amount = parse_positive_number(stock_value)
    if stock_amount is not None:
        return "股票股利"

    cash_amount = parse_positive_number(cash_value)
    if cash_amount is not None:
        return f"現金股利{cash_amount:.2f}"

    return "股利發放"


def fetch_yahoo_calendar_events(
    year: int,
    page_url: str,
    calendar_key: str,
    event_type: str,
    label: str,
) -> List[EventItem]:
    selected_dates = build_yahoo_selected_dates(year)
    if not selected_dates:
        return []

    events: List[EventItem] = []
    seen_keys: set = set()

    for idx, selected_date in enumerate(selected_dates):
        if idx > 0 and YAHOO_REQUEST_DELAY_SECONDS > 0:
            time_module.sleep(YAHOO_REQUEST_DELAY_SECONDS)

        state = fetch_yahoo_state_for_date(page_url, selected_date)
        if not state:
            continue

        calendars = extract_yahoo_calendars(state)

        for _, day_data in calendars.items():
            if not isinstance(day_data, dict):
                continue

            rows = day_data.get(calendar_key) or []
            if not isinstance(rows, list):
                continue

            for item in rows:
                if not isinstance(item, dict):
                    continue

                stock_symbol = clean_text(item.get("symbol"))
                stock_name = clean_text(item.get("symbolName"))
                date_text = clean_text(item.get("date")) or clean_text(item.get("exDate"))

                stock_id = normalize_yahoo_symbol(stock_symbol)
                iso_date, iso_time = parse_yahoo_datetime_parts(date_text)
                event_id = clean_text(item.get("eventId"))

                if not stock_id or not stock_name or not iso_date:
                    continue
                if not iso_date.startswith(str(year)):
                    continue

                dedupe_key = event_id or f"{stock_id}|{iso_date}|{event_type}"
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)

                events.append(
                    EventItem(
                        stock_id=stock_id,
                        stock_name=stock_name,
                        type=event_type,
                        title=f"{stock_id} {stock_name}({label})",
                        date=iso_date,
                        source="Yahoo",
                        url=page_url,
                        time=iso_time if event_type in {"earnings_call", "shareholder_meeting"} else "",
                    )
                )

    return events


def fetch_yahoo_dividend(year: int) -> List[EventItem]:
    selected_dates = build_yahoo_selected_dates(year)
    if not selected_dates:
        return []

    events: List[EventItem] = []
    seen_keys: set = set()

    for idx, selected_date in enumerate(selected_dates):
        if idx > 0 and YAHOO_REQUEST_DELAY_SECONDS > 0:
            time_module.sleep(YAHOO_REQUEST_DELAY_SECONDS)

        state = fetch_yahoo_state_for_date(YAHOO_DIVIDEND_URL, selected_date)
        if not state:
            continue

        calendars = extract_yahoo_calendars(state)

        for _, day_data in calendars.items():
            if not isinstance(day_data, dict):
                continue

            rows = day_data.get("dividend") or []
            if not isinstance(rows, list):
                continue

            for item in rows:
                if not isinstance(item, dict):
                    continue

                stock_symbol = clean_text(item.get("symbol"))
                stock_name = clean_text(item.get("symbolName"))
                stock_id = normalize_yahoo_symbol(stock_symbol)
                event_id = clean_text(item.get("eventId"))
                if not stock_id or not stock_name:
                    continue

                ex_date, _ = parse_yahoo_datetime_parts(clean_text(item.get("date")) or clean_text(item.get("exDate")))
                pay_date, _ = parse_yahoo_datetime_parts(clean_text(item.get("payDate")))
                cash_dividend = format_cash_dividend(item.get("cash"))
                payout_text = build_dividend_payment_text(item.get("cash"), item.get("stock"))

                if ex_date and ex_date.startswith(str(year)):
                    ex_key = f"{event_id}|dividend|{ex_date}" if event_id else f"{stock_id}|dividend|{ex_date}"
                    if ex_key not in seen_keys:
                        seen_keys.add(ex_key)
                        events.append(
                            EventItem(
                                stock_id=stock_id,
                                stock_name=stock_name,
                                type="dividend",
                                title=f"{stock_id} {stock_name}(除權息)",
                                date=ex_date,
                                source="Yahoo",
                                url=YAHOO_DIVIDEND_URL,
                                ex_date=ex_date,
                                pay_date=pay_date or "",
                                cash_dividend=cash_dividend,
                            )
                        )

                if pay_date and pay_date.startswith(str(year)):
                    pay_key = (
                        f"{event_id}|dividend_payment|{pay_date}" if event_id else f"{stock_id}|dividend_payment|{pay_date}"
                    )
                    if pay_key not in seen_keys:
                        seen_keys.add(pay_key)
                        events.append(
                            EventItem(
                                stock_id=stock_id,
                                stock_name=stock_name,
                                type="dividend_payment",
                                title=f"{stock_id} {stock_name} {payout_text}",
                                date=pay_date,
                                source="Yahoo",
                                url=YAHOO_DIVIDEND_URL,
                                ex_date=ex_date or "",
                                pay_date=pay_date,
                                cash_dividend=cash_dividend,
                            )
                        )

    return events


def fetch_yahoo_earnings(year: int) -> List[EventItem]:
    return fetch_yahoo_calendar_events(
        year=year,
        page_url=YAHOO_EARNINGS_URL,
        calendar_key="earningsCall",
        event_type="earnings_call",
        label="法說會",
    )


def fetch_yahoo_holders(year: int) -> List[EventItem]:
    return fetch_yahoo_calendar_events(
        year=year,
        page_url=YAHOO_HOLDERS_MEETING_URL,
        calendar_key="shareHoldersMeeting",
        event_type="shareholder_meeting",
        label="股東會",
    )


def fetch_yahoo_delisting(year: int) -> List[EventItem]:
    return fetch_yahoo_calendar_events(
        year=year,
        page_url=YAHOO_DELISTING_URL,
        calendar_key="delisting",
        event_type="delisting",
        label="終止掛牌",
    )


# -------------------------
# 輸出
# -------------------------
def build(events: List[EventItem], year: int) -> Dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    now_tpe = now_utc.astimezone(TAIPEI_TZ)

    unique: Dict[tuple, EventItem] = {}
    for e in events:
        unique[(e.stock_id, e.date, e.type, e.time, e.title)] = e

    final = []
    for event in unique.values():
        item = asdict(event)
        # Keep the existing event schema stable. These fields are meaningful
        # only for an active disposition and are intentionally absent elsewhere.
        if event.type != "disposition":
            item.pop("disposition_start_date", None)
            item.pop("disposition_end_date", None)
        final.append(item)

    final = sorted(
        final,
        key=lambda x: (x["date"], x["stock_id"], x["type"], x.get("time", ""), x["stock_name"]),
    )

    return {
        "year": year,
        "updated_at_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "updated_at_taipei": now_tpe.strftime("%Y-%m-%d %H:%M:%S UTC+8"),
        "events": final,
    }


def main() -> None:
    args = parse_args()
    year = args.year

    events: List[EventItem] = []
    query_range = get_yahoo_query_range_for_year(year)
    target_start = date(year, 1, 1)
    target_end = date(year, 12, 31)

    if not query_range:
        print(f"[warn] yahoo date range does not cover {year}.")
    else:
        query_start, query_end = query_range
        if query_start > target_start or query_end < target_end:
            print(
                f"[info] yahoo available range for {year}: "
                f"{query_start.isoformat()} ~ {query_end.isoformat()}"
            )

    try:
        if USE_TWSE_DIVIDEND_API:
            events += fetch_dividend(year)
        else:
            events += fetch_yahoo_dividend(year)
    except Exception as e:
        if USE_TWSE_DIVIDEND_API:
            print(f"[warn] twse dividend failed: {e}")
        else:
            print(f"[warn] yahoo dividend failed: {e}")

    if USE_MOPS_CALENDAR:
        try:
            events += fetch_mops(year)
        except Exception as e:
            print(f"[warn] mops failed: {e}")

    try:
        events += fetch_yahoo_holders(year)
    except Exception as e:
        print(f"[warn] yahoo holders meeting failed: {e}")

    try:
        events += fetch_yahoo_earnings(year)
    except Exception as e:
        print(f"[warn] yahoo earnings failed: {e}")

    try:
        events += fetch_yahoo_delisting(year)
    except Exception as e:
        print(f"[warn] yahoo delisting failed: {e}")

    for source_name, fetcher in (
        ("TWSE disposition", fetch_twse_disposition_events),
        ("TPEx disposition", fetch_tpex_disposition_events),
    ):
        try:
            events += fetcher()
        except Exception as e:
            print(f"[warn] {source_name} failed: {e}")

    data = build(events, year)

    OUTPUT_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("done:", len(data["events"]))


if __name__ == "__main__":
    main()
