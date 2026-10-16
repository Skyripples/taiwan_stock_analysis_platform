from __future__ import annotations

import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


class CryptoFrontendTests(unittest.TestCase):
    def test_crypto_page_and_mobile_ui(self):
        html=(ROOT/"crypto.html").read_text(encoding="utf-8")
        script=(ROOT/"crypto.js").read_text(encoding="utf-8")
        css=(ROOT/"crypto.css").read_text(encoding="utf-8")
        for text in ("BTC-USD","ETH-USD","24h High","24h Low","24h Volume","Coinbase Exchange","5 分鐘完成 K 線"):
            self.assertIn(text,html)
        for range_key in ("1H","4H","24H","7D","30D"):
            self.assertIn(f'data-range="{range_key}"',html)
        self.assertIn("/crypto/summary",script); self.assertIn("/candles?range=",script)
        self.assertIn("AbortController",script); self.assertIn("5 分鐘歷史行情建置中",script)
        self.assertIn("@media(max-width:430px)",css); self.assertIn("overflow:hidden",css)
        self.assertIn("crypto.html",(ROOT/"sidebar.js").read_text(encoding="utf-8"))
        self.assertIn("stroke:var(--accent-color,#4d91f7)",css)
        self.assertIn("crypto.css?v=4.3.5",html)


if __name__=="__main__": unittest.main()
