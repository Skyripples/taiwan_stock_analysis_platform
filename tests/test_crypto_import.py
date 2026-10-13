from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))

import import_crypto_history
from crypto_market import validate_candle
from import_crypto_history import staged_rows

UTC=timezone.utc


def raw(symbol="BTC-USD", timestamp=None):
    moment=timestamp or datetime(2026,1,1,tzinfo=UTC)
    return {"timestamp_utc":moment.isoformat(),"interval_seconds":300,"open":100.0,"high":102.0,"low":99.0,"close":101.0,"volume":5.0,
            "timezone":"UTC","available_at":(moment+timedelta(minutes=5)).isoformat(),"quality_flags":"{}",
            "metadata":json.dumps({"product":symbol}),"symbol":symbol,"source":"Coinbase Exchange","fetched_at":"2026-10-13T00:00:00+00:00"}


class CryptoImportTests(unittest.TestCase):
    def fixture(self, directory, rows):
        path=Path(directory)/"symbol=BTC-USD"/"year=2026"/"month=01"/"candles.parquet"
        path.parent.mkdir(parents=True); pq.write_table(pa.Table.from_pylist(rows),path); return path

    def test_dry_run_is_idempotent_and_uses_no_database(self):
        options=argparse.Namespace(symbols=["BTC-USD"],start=None,end=None,batch_size=100,dry_run=True,verify=False)
        with tempfile.TemporaryDirectory() as directory:
            self.fixture(directory,[raw()])
            with patch.object(import_crypto_history,"ROOT",Path(directory)),patch.object(import_crypto_history,"connect") as connect:
                first=import_crypto_history.run(options); second=import_crypto_history.run(options)
        connect.assert_not_called(); self.assertEqual(first["rows"],1); self.assertEqual(first,second)
        self.assertEqual(first["interval_seconds"],300); self.assertEqual(first["source"],"coinbase")

    def test_utc_source_metadata_and_null_is_not_zero(self):
        item=raw(); item["volume"]=None
        with self.assertRaises((TypeError,ValueError)): validate_candle(item)
        with tempfile.TemporaryDirectory() as directory:
            path=self.fixture(directory,[raw()])
            batches=list(staged_rows([path],{"BTC-USD"},None,None,100))
        row=batches[0][0]
        self.assertEqual(row["timestamp_utc"].utcoffset(),timedelta(0))
        self.assertEqual(row["available_at"].utcoffset(),timedelta(0))
        self.assertEqual(row["interval_seconds"],300)
        self.assertEqual(row["metadata"]["source_key"],"coinbase")
        self.assertEqual(row["metadata"]["source_name"],"Coinbase Exchange")
        self.assertEqual(row["metadata"]["source_fetched_at"],"2026-10-13T00:00:00+00:00")

    def test_date_filter_and_duplicate_fixture_are_stable(self):
        moment=datetime(2026,1,1,tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            self.fixture(directory,[raw(timestamp=moment),raw(timestamp=moment),raw(timestamp=moment+timedelta(minutes=5))])
            options=argparse.Namespace(symbols=["BTC-USD"],start="2026-01-01",end="2026-01-01",batch_size=100,dry_run=True,verify=False)
            with patch.object(import_crypto_history,"ROOT",Path(directory)):
                result=import_crypto_history.run(options)
        self.assertEqual(result["rows"],2); self.assertEqual(result["duplicates"],1)

    def test_import_commits_lookup_and_batch(self):
        class Cursor:
            def __enter__(self): return self
            def __exit__(self,*_): return False
            def execute(self,*_): pass
            def fetchall(self): return [{"source_symbol":"BTC-USD","instrument_id":7}]
        class Connection:
            def __init__(self): self.commits=0
            def cursor(self): return Cursor()
            def commit(self): self.commits+=1
            def rollback(self): pass
            def close(self): pass
        class Repository:
            def __init__(self,_): self.rows=[]
            def upsert_many(self,table,rows): self.rows.extend(rows); return len(rows)
        options=argparse.Namespace(symbols=["BTC-USD"],start=None,end=None,batch_size=100,dry_run=False,verify=False)
        connection=Connection()
        with tempfile.TemporaryDirectory() as directory:
            self.fixture(directory,[raw()])
            with patch.object(import_crypto_history,"ROOT",Path(directory)),patch.object(import_crypto_history,"connect",return_value=connection),patch.object(import_crypto_history,"GlobalMarketRepository",Repository):
                result=import_crypto_history.run(options)
        self.assertEqual(result["rows"],1); self.assertEqual(connection.commits,2)


if __name__=="__main__": unittest.main()
