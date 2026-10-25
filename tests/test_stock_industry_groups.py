from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import update_stock_industry_groups as groups
from scripts.update_stock_industry_groups import parse_catalog_codes, parse_chain_page, payload_from


class IndustryGroupTests(unittest.TestCase):
    def test_catalog_codes_deduplicate(self):
        self.assertEqual(parse_catalog_codes('introduce.php?ic=L000 introduce.php?ic=L000 introduce.php?ic=D000'), ["D000", "L000"])

    def test_company_can_belong_to_multiple_full_paths(self):
        document = '''<h3>印刷電路板產業鏈簡介</h3>
        <div id="companyList_L610" title="硬板、軟板、IC載板製造"><a href="company_basic.php?stk_code=3037">欣興</a></div>
        <div id="companyList_L620" title="基板組裝加工及相關製造"><a href="company_basic.php?stk_code=3037">欣興</a></div>'''
        rows = parse_chain_page(document, "https://example.test/L000")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["group_path"] for row in rows}, {"印刷電路板 > 硬板、軟板、IC載板製造", "印刷電路板 > 基板組裝加工及相關製造"})

    def test_payload_deduplicates_associations_and_builds_reverse_index(self):
        row = {"symbol": "3037", "group_key": "tpex-a", "group_name": "印刷電路板", "segment_name": "硬板", "group_path": "印刷電路板 > 硬板", "source": "TPEx Industry Value Chain", "source_url": "https://example.test", "is_official": True}
        payload = payload_from([row], 48)
        self.assertEqual(payload["by_symbol"]["3037"], ["tpex-a"])
        self.assertEqual(payload["by_group"]["tpex-a"], ["3037"])
        self.assertTrue(payload["complete_snapshot"])

    def test_existing_official_industry_is_not_touched(self):
        # The importer reads only index symbols and writes a separate dimension.
        source = Path("scripts/update_stock_industry_groups.py").read_text(encoding="utf-8")
        self.assertNotIn('"industry" =', source)
        self.assertIn("industry-groups.json", source)

    def test_etf_and_unknown_symbols_are_excluded_from_snapshot(self):
        class Response:
            def __init__(self, text): self.text = text
            def raise_for_status(self): pass
            encoding = "utf-8"
        class Session:
            def get(self, url, timeout):
                if url.endswith("/"):
                    return Response('introduce.php?ic=L000')
                return Response('''<h3>印刷電路板產業鏈簡介</h3><div id="companyList_L610" title="硬板"><a href="company_basic.php?stk_code=3037">欣興</a><a href="company_basic.php?stk_code=0050">ETF</a><a href="company_basic.php?stk_code=9999">未知</a></div>''')
        with patch.object(groups, "universe_symbols", return_value={"3037"}):
            rows, _ = groups.build_snapshot(Session(), timeout=1, delay=0)
        self.assertEqual([row["symbol"] for row in rows], ["3037"])

    def test_normalized_schema_has_many_to_many_primary_key(self):
        migration = Path("scripts/database/migrations/018_stock_industry_groups.sql").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS industry_groups", migration)
        self.assertIn("CREATE TABLE IF NOT EXISTS stock_industry_groups", migration)
        self.assertIn("PRIMARY KEY (stock_id, group_id)", migration)

    def test_reconcile_requires_complete_snapshot(self):
        source = Path("scripts/sync_stock_database.py").read_text(encoding="utf-8")
        self.assertIn('payload.get("complete_snapshot")', source)
        self.assertIn('DELETE FROM stock_industry_groups', source)


if __name__ == "__main__":
    unittest.main()
