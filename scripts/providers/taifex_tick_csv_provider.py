"""TAIFEX official daily tick CSV downloader and streaming parser."""
from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import BinaryIO, Iterable, Iterator

import requests

from futures_intraday import FuturesTick, TAIPEI

SOURCE = "TAIFEX official daily tick CSV"
PAGE_URL = "https://www.taifex.com.tw/cht/3/futPrevious30DaysSalesData"
ZIP_URL = "https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_{date}.zip"
PRODUCTS = ("TX", "MTX", "TMF")
HEADER = ("成交日期", "商品代號", "到期月份(週別)", "成交時間", "成交價格", "成交數量(B+S)")


class OfficialTickFileUnavailable(RuntimeError):
    pass


class OfficialTickFormatError(RuntimeError):
    pass


@dataclass(frozen=True)
class DownloadedTickFile:
    trading_date: date
    content: bytes
    available_at: datetime


def official_url(trading_date: date) -> str:
    return ZIP_URL.format(date=trading_date.strftime("%Y_%m_%d"))


def _response_available_at(response) -> datetime:
    value = response.headers.get("Last-Modified")
    if value:
        try:
            return parsedate_to_datetime(value).astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
    return datetime.now(timezone.utc)


def download_tick_zip(trading_date: date, *, session: requests.Session | None = None,
                      timeout: tuple[int, int] = (10, 60)) -> DownloadedTickFile:
    client = session or requests.Session()
    response = client.get(official_url(trading_date), timeout=timeout, headers={
        "User-Agent": "taiwan-stock-analysis-platform/4.3 official-tick-reconstruction",
        "Accept": "application/zip,application/octet-stream", "Referer": PAGE_URL,
    })
    if response.status_code in (404, 410):
        raise OfficialTickFileUnavailable(f"TAIFEX tick file is not published for {trading_date}")
    response.raise_for_status()
    if not response.content.startswith(b"PK"):
        raise OfficialTickFormatError("TAIFEX tick response is not a ZIP file")
    return DownloadedTickFile(trading_date, response.content, _response_available_at(response))


def _decode(stream: BinaryIO) -> io.TextIOWrapper:
    sample = stream.read(4096); stream.seek(0)
    try:
        sample.decode("utf-8-sig"); encoding = "utf-8-sig"
    except UnicodeDecodeError:
        encoding = "cp950"
    return io.TextIOWrapper(stream, encoding=encoding, newline="", errors="strict")


def iter_csv_rows(zip_content: bytes) -> Iterator[dict[str, str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_content))
    except zipfile.BadZipFile as exc:
        raise OfficialTickFormatError("Invalid TAIFEX ZIP") from exc
    names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
    if len(names) != 1:
        raise OfficialTickFormatError("TAIFEX ZIP must contain exactly one CSV")
    with archive.open(names[0]) as binary, _decode(binary) as text:
        reader = csv.DictReader(text)
        fields = tuple((name or "").strip() for name in (reader.fieldnames or ()))
        if not set(HEADER).issubset(fields):
            raise OfficialTickFormatError(f"Unexpected TAIFEX tick header: {fields}")
        for row in reader:
            yield {(key or "").strip(): (value or "").strip() for key, value in row.items()}


def parse_tick_row(row: dict[str, str]) -> FuturesTick | None:
    product = row.get("商品代號", "").strip()
    if product not in PRODUCTS:
        return None
    month = row.get("到期月份(週別)", "").strip()
    if len(month) != 6 or not month.isdigit():
        return None
    raw_date = row.get("成交日期", "").strip()
    raw_time = row.get("成交時間", "").strip().zfill(6)
    try:
        timestamp = datetime.strptime(raw_date + raw_time, "%Y%m%d%H%M%S").replace(tzinfo=TAIPEI)
        price = Decimal(row["成交價格"].replace(",", ""))
        volume = int(row["成交數量(B+S)"].replace(",", ""))
    except (KeyError, ValueError, InvalidOperation) as exc:
        raise OfficialTickFormatError(f"Malformed {product} tick row") from exc
    if price <= 0 or volume < 0:
        raise OfficialTickFormatError(f"Invalid {product} price/volume")
    return FuturesTick(timestamp, product, f"{product}{month}", price, volume)


def iter_product_ticks(zip_content: bytes, products: Iterable[str] = PRODUCTS) -> Iterator[FuturesTick]:
    selected = set(products)
    for row in iter_csv_rows(zip_content):
        tick = parse_tick_row(row)
        if tick is not None and tick.product in selected:
            yield tick
