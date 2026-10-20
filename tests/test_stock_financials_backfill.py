from __future__ import annotations
import argparse,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"scripts"))
import backfill_stock_history as backfill
import update_stock_financials as financials
from stock_history_metrics import financial_metrics


class FixtureClient:
    def __init__(self):self.calls=0
    def rows(self,endpoint,symbol,year,quarter):
        self.calls+=1
        if endpoint==financials.ENDPOINTS["income"]:
            keys=("revenue","gross_profit","operating_income","net_income","eps")
        elif endpoint==financials.ENDPOINTS["balance"]:
            keys=("current_assets","total_assets","current_liabilities","total_liabilities","total_equity")
        else:
            keys=("operating_cash_flow_cumulative","investing_cash_flow_cumulative","financing_cash_flow_cumulative","capital_expenditure_cumulative_raw")
        return [[financials.ALIASES[key][0],str(1000 if key not in {"total_assets","total_equity"} else 5000)] for key in keys]


class StockFinancialsBackfillTests(unittest.TestCase):
    def test_component_checkpoint_survives_interrupt_and_resume(self):
        class Client:
            def __init__(self):self.calls=[]
            def rows(self,endpoint,symbol,year,quarter):
                self.calls.append(endpoint)
                if endpoint=="income-endpoint":raise KeyboardInterrupt()
                return [["field",None]]
        with tempfile.TemporaryDirectory() as directory,patch.object(backfill,"STAGING",Path(directory)):
            client=Client()
            balance=backfill.load_or_fetch_financial_component(client,"2330",2024,1,"balance","balance-endpoint")
            self.assertEqual(balance,[["field",None]])
            with self.assertRaises(KeyboardInterrupt):backfill.load_or_fetch_financial_component(client,"2330",2024,1,"income","income-endpoint")
            resumed=Client()
            self.assertEqual(backfill.load_or_fetch_financial_component(resumed,"2330",2024,1,"balance","balance-endpoint"),balance)
            self.assertEqual(resumed.calls,[])
            self.assertTrue(backfill.financial_component_path("2330",2024,1,"balance").exists())
            self.assertFalse(backfill.financial_component_path("2330",2024,1,"income").exists())

    def test_only_missing_cash_flow_is_requested_and_valid_checkpoint_not_overwritten(self):
        class Client:
            def __init__(self,fail=None):self.calls=[];self.fail=fail
            def rows(self,endpoint,*args):
                self.calls.append(endpoint)
                if endpoint==self.fail:raise RuntimeError("interrupted component")
                return [[endpoint,"1"]]
        with tempfile.TemporaryDirectory() as directory,patch.object(backfill,"STAGING",Path(directory)):
            first=Client(fail="cash_flow")
            for component in ("balance","income"):
                backfill.load_or_fetch_financial_component(first,"2330",2024,2,component,component)
            with self.assertRaises(RuntimeError):backfill.load_or_fetch_financial_component(first,"2330",2024,2,"cash_flow","cash_flow")
            second=Client()
            balance=backfill.load_or_fetch_financial_component(second,"2330",2024,2,"balance","balance")
            income=backfill.load_or_fetch_financial_component(second,"2330",2024,2,"income","income")
            cash=backfill.load_or_fetch_financial_component(second,"2330",2024,2,"cash_flow","cash_flow")
            self.assertEqual(second.calls,["cash_flow"])
            self.assertEqual(balance,[["balance","1"]]);self.assertEqual(income,[["income","1"]]);self.assertEqual(cash,[["cash_flow","1"]])

    def test_dependency_quarters_use_component_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(backfill,"STAGING",Path(directory)):
            client=FixtureClient()
            loader=lambda c,s,y,q,n,e:backfill.load_or_fetch_financial_component(c,s,y,q,n,e)
            financials.fetch_symbol(client,"2330",backfill.financial_dependency_quarters([(2024,3)]),component_loader=loader)
            self.assertEqual(client.calls,9)
            again=FixtureClient()
            financials.fetch_symbol(again,"2330",backfill.financial_dependency_quarters([(2024,3)]),component_loader=loader)
            self.assertEqual(again.calls,0)
            for quarter in (1,2,3):
                for component in financials.ENDPOINTS:self.assertTrue(backfill.financial_component_path("2330",2024,quarter,component).exists())

    def test_q4_and_q3_dependencies_are_exact(self):
        self.assertEqual(backfill.financial_dependency_quarters([(2024,4)]),[(2024,1),(2024,2),(2024,3),(2024,4)])
        self.assertEqual(backfill.financial_dependency_quarters([(2025,3)]),[(2025,1),(2025,2),(2025,3)])

    def test_dependency_outside_requested_range_is_fetched_but_not_persisted(self):
        options=argparse.Namespace(dataset="financials",start="2024-10-01",end="2024-12-31",years=1,symbols=["2330"],resume=True,dry_run=False,refresh=False,audit_only=False,delay=0)
        client=FixtureClient()
        with tempfile.TemporaryDirectory() as directory,patch.object(backfill,"STAGING",Path(directory)),patch.object(backfill,"arguments",return_value=options),patch.object(backfill,"universe",return_value=({"2330":"TWSE"},{})),patch("update_stock_financials.recent_quarters",return_value=[(2024,4)]),patch("update_stock_financials.MopsClient",return_value=client):
            self.assertEqual(backfill.main(),0)
            self.assertEqual(client.calls,12)
            rows=backfill.read_rows(backfill.partition("financials","2330"))
            self.assertEqual([(row["fiscal_year"],row["quarter"]) for row in rows],[(2024,4)])

    def test_quarter_failure_does_not_discard_later_success(self):
        class Client(FixtureClient):
            def rows(self,endpoint,symbol,year,quarter):
                rows=super().rows(endpoint,symbol,year,quarter)
                if quarter==2 and endpoint==financials.ENDPOINTS["income"]:
                    return [row for row in rows if row[0]!=financials.ALIASES["revenue"][0]]
                return rows
        with tempfile.TemporaryDirectory() as directory,patch.object(backfill,"STAGING",Path(directory)):
            status={"version":1,"quarters":{}}
            result=backfill.process_financial_symbol(Client(),"2330",[(2025,1),(2025,2),(2025,3)],
                                                     {(2025,1),(2025,2),(2025,3)},[],status)
            rows=backfill.read_rows(backfill.partition("financials","2330"))
            self.assertEqual([(row["fiscal_year"],row["quarter"]) for row in rows],[(2025,1),(2025,3)])
            self.assertEqual(result["downloaded_rows"],2)
            self.assertEqual(status["quarters"]["2330|2025Q2"]["status"],"parser_schema_error")
            self.assertEqual(status["quarters"]["2330|2025Q3"]["status"],"success")

    def test_network_and_official_empty_are_quarter_scoped(self):
        class Client(FixtureClient):
            def rows(self,endpoint,symbol,year,quarter):
                if quarter==2 and endpoint==financials.ENDPOINTS["balance"]:raise RuntimeError("502 Server Error")
                if quarter==3 and endpoint==financials.ENDPOINTS["balance"]:raise RuntimeError("MOPS statement table is empty")
                return super().rows(endpoint,symbol,year,quarter)
        with tempfile.TemporaryDirectory() as directory,patch.object(backfill,"STAGING",Path(directory)):
            status={"version":1,"quarters":{}}
            backfill.process_financial_symbol(Client(),"2330",[(2025,1),(2025,2),(2025,3)],
                                              {(2025,1),(2025,2),(2025,3)},[],status)
            self.assertEqual(status["quarters"]["2330|2025Q1"]["status"],"success")
            self.assertEqual(status["quarters"]["2330|2025Q2"]["status"],"retryable_error")
            self.assertEqual(status["quarters"]["2330|2025Q3"]["status"],"official_empty")
            rows=backfill.read_rows(backfill.partition("financials","2330"))
            self.assertEqual([(row["fiscal_year"],row["quarter"]) for row in rows],[(2025,1)])

    def test_quarter_rerun_is_idempotent_and_existing_row_is_not_refetched(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(backfill,"STAGING",Path(directory)):
            status={"version":1,"quarters":{}};client=FixtureClient()
            first=backfill.process_financial_symbol(client,"2330",[(2025,1)],{(2025,1)},[],status)
            existing=backfill.read_rows(backfill.partition("financials","2330"));calls=client.calls
            second=backfill.process_financial_symbol(client,"2330",[(2025,1)],{(2025,1)},existing,status)
            rows=backfill.read_rows(backfill.partition("financials","2330"))
            self.assertEqual(first["downloaded_rows"],1);self.assertEqual(second["downloaded_rows"],0)
            self.assertEqual(second["skipped_existing"],1);self.assertEqual(client.calls,calls)
            self.assertEqual(len(rows),1)

    def test_missing_q4_and_cash_flow_dependencies_do_not_create_values(self):
        statements={}
        client=FixtureClient()
        for component,endpoint in financials.ENDPOINTS.items():
            statements[component]=client.rows(endpoint,"2330",2025,4)
        raw,missing=financials.parse_quarter_statements("2330",2025,4,statements)
        self.assertEqual(missing,[])
        with self.assertRaises(financials.FinancialDependencyMissing):
            financials.derive_quarter("2330",raw,{(2025,1):raw,(2025,3):raw})

        q3=dict(raw,fiscal_year=2025,quarter=3,income_period_basis="standalone_quarter")
        with self.assertRaises(financials.FinancialDependencyMissing):
            financials.derive_quarter("2330",q3,{})

        q1=dict(raw,fiscal_year=2025,quarter=1,income_period_basis="standalone_quarter")
        derived,flags=financials.derive_quarter("2330",q1,{})
        self.assertEqual(flags,[]);self.assertIsNone(derived["roe"])
        self.assertIn("previous_equity_unavailable",derived["quality_flags"])
    def test_parse_null_parentheses_and_pit_unknown(self):
        self.assertIsNone(financials.number("--"));self.assertEqual(financials.number("(123)"),-123)
        client=FixtureClient();rows=financials.fetch_symbol(client,"2330",[(2025,1)])
        self.assertEqual(len(rows),1);self.assertIsNone(rows[0]["available_date"]);self.assertIsNone(rows[0]["available_at"])
        self.assertEqual(rows[0]["source_date"],"2025-03-31");self.assertEqual(rows[0]["equity"],5000)
        self.assertIsNotNone(rows[0]["financing_cash_flow"])

    def test_derived_metrics_null_safe(self):
        row=financial_metrics({"revenue":100,"gross_profit":40,"operating_income":20,"net_income":10,"total_assets":200,"total_liabilities":80,"equity":120,"operating_cash_flow":30,"capital_expenditure":12})
        self.assertEqual(row["gross_margin"],40);self.assertEqual(row["operating_margin"],20);self.assertEqual(row["net_margin"],10)
        self.assertEqual(row["debt_ratio"],40);self.assertEqual(row["free_cash_flow"],18);self.assertIsNotNone(row["roe"]);self.assertIsNotNone(row["roa"])
        self.assertIsNone(financial_metrics({})["free_cash_flow"])

    def test_complete_resume_is_zero_network_and_idempotent(self):
        options=argparse.Namespace(dataset="financials",start="2024-01-01",end="2026-01-01",years=1,symbols=["2330"],resume=True,dry_run=False,refresh=False,audit_only=False,delay=0)
        cached=[{"symbol":"2330","fiscal_year":2025,"quarter":1,"statement_scope":"consolidated","period_end":"2025-03-31","available_date":None,"revenue":1},{"symbol":"2330","fiscal_year":2025,"quarter":2,"statement_scope":"consolidated","period_end":"2025-06-30","available_date":None,"revenue":2}]
        client=FixtureClient()
        with tempfile.TemporaryDirectory() as directory,patch.object(backfill,"STAGING",Path(directory)),patch.object(backfill,"arguments",return_value=options),patch.object(backfill,"universe",return_value=({"2330":"TWSE"},{})),patch("update_stock_financials.recent_quarters",return_value=[(2025,1),(2025,2)]),patch("update_stock_financials.MopsClient",return_value=client):
            backfill.write_rows(backfill.partition("financials","2330"),cached,("symbol","fiscal_year","quarter","statement_scope"))
            self.assertEqual(backfill.main(),0);self.assertEqual(client.calls,0)
            self.assertEqual(backfill.main(),0);self.assertEqual(client.calls,0)
            summary=json.loads((Path(directory)/"summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["downloaded_rows"],0);self.assertEqual(summary["skipped_existing"],2);self.assertEqual(summary["duplicates"],0)
            self.assertEqual(len(backfill.read_rows(backfill.partition("financials","2330"))),2)

    def test_offline_audit_detects_coverage_without_network(self):
        rows=[{"symbol":"2330","fiscal_year":2025,"quarter":1,"statement_scope":"consolidated","period_end":"2025-03-31","source":"MOPS consolidated IFRS statements"}]
        with tempfile.TemporaryDirectory() as directory,patch.object(backfill,"STAGING",Path(directory)),patch("update_stock_financials.recent_quarters",return_value=[(2025,1),(2025,2)]):
            backfill.write_rows(backfill.partition("financials","2330"),rows,("symbol","fiscal_year","quarter","statement_scope"))
            audit=backfill.audit_financials_staging({"2330":"TWSE"},2)
            self.assertEqual(audit["duplicate_keys"],0);self.assertEqual(audit["missing_quarters"]["2330"],["2025Q2"])
            self.assertEqual(audit["source_coverage"]["MOPS consolidated IFRS statements"],1)

    def test_schema_is_additive_and_allows_unknown_availability(self):
        sql=(ROOT/"scripts/database/migrations/010_stock_financials_expansion.sql").read_text(encoding="utf-8")
        self.assertIn("DROP NOT NULL",sql);self.assertGreaterEqual(sql.count("ADD COLUMN IF NOT EXISTS"),6)


if __name__=="__main__":unittest.main()
