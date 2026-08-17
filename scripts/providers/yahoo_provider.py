"""Yahoo Finance daily market data providers."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import requests

from .base_provider import BaseProvider, NormalizedRecords


Number = int | float


class YahooProvider(BaseProvider):
    """Fetch the latest completed NYSE daily candle for TSM ADR."""

    name = "Yahoo"
    dataset = "tsm_adr"
    output_filename = "tsm_adr.json"
    enabled = True

    symbol = "TSM"
    security_name = "Taiwan Semiconductor Manufacturing Company Limited ADR"
    expected_currency = "USD"
    source_url = "https://finance.yahoo.com/quote/TSM/history/"
    chart_url = "https://query1.finance.yahoo.com/v8/finance/chart/TSM"
    request_timeout_seconds = 30

    def fetch(self) -> Any:
        response = requests.get(
            self.chart_url,
            params={
                "interval": "1d",
                "range": "1mo",
                "events": "history",
                "includeAdjustedClose": "false",
            },
            headers={
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": "Mozilla/5.0 (compatible; taiwan-stock-analysis-platform/1.0)",
            },
            timeout=self.request_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        chart = payload.get("chart")
        if not isinstance(chart, dict) or chart.get("error") is not None:
            raise ValueError(f"Yahoo Finance chart error: {chart.get('error') if isinstance(chart, dict) else 'invalid response'}")
        return {
            "fetched_at": time.time(),
            "chart": chart,
        }

    def normalize(self, raw_data: Any) -> NormalizedRecords:
        if not isinstance(raw_data, dict):
            raise TypeError("Yahoo raw data must be an object")
        chart = raw_data.get("chart")
        fetched_at = raw_data.get("fetched_at")
        results = chart.get("result") if isinstance(chart, dict) else None
        if not isinstance(results, list) or len(results) != 1:
            raise ValueError("Yahoo response is missing a unique chart result")
        if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
            raise ValueError("Yahoo response is missing fetch time")

        result = results[0]
        meta = result.get("meta") if isinstance(result, dict) else None
        timestamps = result.get("timestamp") if isinstance(result, dict) else None
        indicators = result.get("indicators") if isinstance(result, dict) else None
        quotes = indicators.get("quote") if isinstance(indicators, dict) else None
        if not isinstance(meta, dict) or not isinstance(timestamps, list):
            raise ValueError("Yahoo response is missing metadata or timestamps")
        if not isinstance(quotes, list) or len(quotes) != 1 or not isinstance(quotes[0], dict):
            raise ValueError("Yahoo response is missing daily quote data")

        if meta.get("symbol") != self.symbol:
            raise ValueError("Yahoo response returned an unexpected symbol")
        currency = meta.get("currency")
        market_timezone = meta.get("exchangeTimezoneName")
        exchange = meta.get("fullExchangeName") or meta.get("exchangeName")
        utc_offset = meta.get("gmtoffset")
        if (
            not isinstance(currency, str)
            or not isinstance(market_timezone, str)
            or not isinstance(exchange, str)
            or isinstance(utc_offset, bool)
            or not isinstance(utc_offset, int)
        ):
            raise ValueError("Yahoo response is missing market metadata")

        incomplete_date = self._incomplete_market_date(meta, float(fetched_at), utc_offset)
        candles = self._completed_candles(timestamps, quotes[0], utc_offset, incomplete_date)
        if len(candles) < 2:
            raise ValueError(
                f"Yahoo response has fewer than two completed {self.symbol} trading days"
            )

        latest = candles[-1]
        previous = candles[-2]
        previous_close = previous["close"]
        change = round(latest["close"] - previous_close, 4)
        change_percent = round((change / previous_close) * 100, 4)

        return [
            {
                "trade_date": latest["trade_date"],
                "metadata": {
                    "symbol": self.symbol,
                    "security_name": self.security_name,
                    "market": exchange,
                    "currency": currency,
                    "market_timezone": market_timezone,
                    "interval": "1d",
                    "source": self.source_url,
                    "api_source": self.chart_url,
                },
                "open": latest["open"],
                "high": latest["high"],
                "low": latest["low"],
                "close": latest["close"],
                "previous_close": previous_close,
                "change": change,
                "change_percent": change_percent,
                "volume": latest["volume"],
            }
        ]

    def validate(self, records: NormalizedRecords) -> bool:
        if not isinstance(records, list) or len(records) != 1:
            return False
        record = records[0]
        if not isinstance(record, dict):
            return False

        try:
            datetime.strptime(record["trade_date"], "%Y-%m-%d")
            metadata = record["metadata"]
            if (
                metadata.get("symbol") != self.symbol
                or metadata.get("currency") != self.expected_currency
                or not isinstance(metadata.get("market_timezone"), str)
                or not metadata["market_timezone"]
                or not str(metadata.get("source", "")).startswith("https://finance.yahoo.com/")
            ):
                return False

            open_price = record["open"]
            high_price = record["high"]
            low_price = record["low"]
            close_price = record["close"]
            previous_close = record["previous_close"]
            if not all(
                self._is_number(value) and value > 0
                for value in (open_price, high_price, low_price, close_price, previous_close)
            ):
                return False
            if high_price < low_price or high_price < max(open_price, close_price):
                return False
            if low_price > min(open_price, close_price):
                return False

            change = record["change"]
            change_percent = record["change_percent"]
            if not self._is_number(change) or not self._is_number(change_percent):
                return False
            if abs((close_price - previous_close) - change) > 0.0001:
                return False
            expected_percent = (change / previous_close) * 100
            if abs(expected_percent - change_percent) > 0.0001:
                return False

            volume = record["volume"]
            if isinstance(volume, bool) or not isinstance(volume, int) or volume < 0:
                return False
        except (KeyError, TypeError, ValueError):
            return False
        return True

    @classmethod
    def _completed_candles(
        cls,
        timestamps: List[Any],
        quote: Dict[str, Any],
        utc_offset: int,
        incomplete_date: str | None,
    ) -> List[Dict[str, Any]]:
        fields = ("open", "high", "low", "close", "volume")
        values = {field: quote.get(field) for field in fields}
        if not all(isinstance(values[field], list) for field in fields):
            raise ValueError("Yahoo daily quote arrays are incomplete")

        candles: List[Dict[str, Any]] = []
        for index, timestamp in enumerate(timestamps):
            if isinstance(timestamp, bool) or not isinstance(timestamp, int):
                continue
            if any(index >= len(values[field]) for field in fields):
                continue
            candle_values = {field: values[field][index] for field in fields}
            if not all(cls._is_number(candle_values[field]) for field in fields):
                continue

            trade_date = cls._market_date(timestamp, utc_offset)
            if trade_date == incomplete_date:
                continue
            volume = candle_values["volume"]
            if isinstance(volume, bool) or not isinstance(volume, int) or volume < 0:
                continue
            candles.append(
                {
                    "trade_date": trade_date,
                    "open": cls._clean_price(candle_values["open"]),
                    "high": cls._clean_price(candle_values["high"]),
                    "low": cls._clean_price(candle_values["low"]),
                    "close": cls._clean_price(candle_values["close"]),
                    "volume": volume,
                }
            )
        return sorted(candles, key=lambda candle: candle["trade_date"])

    @classmethod
    def _incomplete_market_date(
        cls, meta: Dict[str, Any], fetched_at: float, utc_offset: int
    ) -> str | None:
        periods = meta.get("currentTradingPeriod")
        regular = periods.get("regular") if isinstance(periods, dict) else None
        if not isinstance(regular, dict):
            raise ValueError("Yahoo response is missing regular trading period")
        start = regular.get("start")
        end = regular.get("end")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
        ):
            raise ValueError("Yahoo regular trading period is invalid")
        if fetched_at < end:
            return cls._market_date(start, utc_offset)
        return None

    @staticmethod
    def _market_date(timestamp: int, utc_offset: int) -> str:
        local_time = datetime.fromtimestamp(timestamp, timezone.utc) + timedelta(seconds=utc_offset)
        return local_time.date().isoformat()

    @staticmethod
    def _clean_price(value: Number) -> Number:
        cleaned = round(float(value), 4)
        return int(cleaned) if cleaned.is_integer() else cleaned

    @staticmethod
    def _is_number(value: Any) -> bool:
        return not isinstance(value, bool) and isinstance(value, (int, float))


class YahooSoxProvider(YahooProvider):
    """Fetch the latest completed Philadelphia Semiconductor Index candle."""

    dataset = "sox_index"
    output_filename = "sox_index.json"

    symbol = "^SOX"
    security_name = "Philadelphia Semiconductor Index"
    source_url = "https://finance.yahoo.com/quote/%5ESOX/history/"
    chart_url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ESOX"


class YahooSp500Provider(YahooProvider):
    """Fetch the latest completed S&P 500 Index candle."""

    dataset = "sp500_index"
    output_filename = "sp500_index.json"

    symbol = "^GSPC"
    security_name = "S&P 500 Index"
    source_url = "https://finance.yahoo.com/quote/%5EGSPC/history/"
    chart_url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"


class YahooNasdaqProvider(YahooProvider):
    """Fetch the latest completed NASDAQ Composite Index candle."""

    dataset = "nasdaq_index"
    output_filename = "nasdaq_index.json"

    symbol = "^IXIC"
    security_name = "NASDAQ Composite Index"
    source_url = "https://finance.yahoo.com/quote/%5EIXIC/history/"
    chart_url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EIXIC"


class YahooVixProvider(YahooProvider):
    dataset = "vix_index"
    output_filename = "vix_index.json"
    symbol = "^VIX"
    security_name = "CBOE Volatility Index"
    source_url = "https://finance.yahoo.com/quote/%5EVIX/history/"
    chart_url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX"


class YahooNikkeiProvider(YahooProvider):
    dataset = "nikkei_225_index"
    output_filename = "nikkei_225_index.json"
    symbol = "^N225"
    security_name = "Nikkei 225 Index"
    expected_currency = "JPY"
    source_url = "https://finance.yahoo.com/quote/%5EN225/history/"
    chart_url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EN225"


class YahooKospiProvider(YahooProvider):
    dataset = "kospi_index"
    output_filename = "kospi_index.json"
    symbol = "^KS11"
    security_name = "KOSPI Composite Index"
    expected_currency = "KRW"
    source_url = "https://finance.yahoo.com/quote/%5EKS11/history/"
    chart_url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EKS11"


class YahooHangSengProvider(YahooProvider):
    dataset = "hang_seng_index"
    output_filename = "hang_seng_index.json"
    symbol = "^HSI"
    security_name = "Hang Seng Index"
    expected_currency = "HKD"
    source_url = "https://finance.yahoo.com/quote/%5EHSI/history/"
    chart_url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EHSI"


class YahooCsi300Provider(YahooProvider):
    dataset = "csi_300_index"
    output_filename = "csi_300_index.json"
    symbol = "000300.SS"
    security_name = "CSI 300 Index"
    expected_currency = "CNY"
    source_url = "https://finance.yahoo.com/quote/000300.SS/history/"
    chart_url = "https://query1.finance.yahoo.com/v8/finance/chart/000300.SS"


class YahooSoxxProvider(YahooProvider):
    dataset = "soxx_etf"
    output_filename = "soxx_etf.json"
    symbol = "SOXX"
    security_name = "iShares Semiconductor ETF"
    source_url = "https://finance.yahoo.com/quote/SOXX/history/"
    chart_url = "https://query1.finance.yahoo.com/v8/finance/chart/SOXX"


class YahooSmhProvider(YahooProvider):
    dataset = "smh_etf"
    output_filename = "smh_etf.json"
    symbol = "SMH"
    security_name = "VanEck Semiconductor ETF"
    source_url = "https://finance.yahoo.com/quote/SMH/history/"
    chart_url = "https://query1.finance.yahoo.com/v8/finance/chart/SMH"


class YahooNvdaProvider(YahooProvider):
    dataset = "nvda_stock"
    output_filename = "nvda_stock.json"
    symbol = "NVDA"
    security_name = "NVIDIA Corporation"
    source_url = "https://finance.yahoo.com/quote/NVDA/history/"
    chart_url = "https://query1.finance.yahoo.com/v8/finance/chart/NVDA"


class YahooAmdProvider(YahooProvider):
    dataset = "amd_stock"
    output_filename = "amd_stock.json"
    symbol = "AMD"
    security_name = "Advanced Micro Devices, Inc."
    source_url = "https://finance.yahoo.com/quote/AMD/history/"
    chart_url = "https://query1.finance.yahoo.com/v8/finance/chart/AMD"


class YahooAvgoProvider(YahooProvider):
    dataset = "avgo_stock"
    output_filename = "avgo_stock.json"
    symbol = "AVGO"
    security_name = "Broadcom Inc."
    source_url = "https://finance.yahoo.com/quote/AVGO/history/"
    chart_url = "https://query1.finance.yahoo.com/v8/finance/chart/AVGO"


class YahooUsdtwdProvider(YahooProvider):
    dataset = "usdtwd"
    output_filename = "usdtwd.json"
    symbol = "TWD=X"
    security_name = "USD/TWD Exchange Rate"
    expected_currency = "TWD"
    source_url = "https://finance.yahoo.com/quote/TWD%3DX/history/"
    chart_url = "https://query1.finance.yahoo.com/v8/finance/chart/TWD%3DX"


class YahooDxyProvider(YahooProvider):
    dataset = "dxy_index"
    output_filename = "dxy_index.json"
    symbol = "DX-Y.NYB"
    security_name = "U.S. Dollar Index"
    source_url = "https://finance.yahoo.com/quote/DX-Y.NYB/history/"
    chart_url = "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB"
