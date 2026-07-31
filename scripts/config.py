"""Configuration shared by future market data update jobs."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MARKET_DATA_DIR = PROJECT_ROOT / "data" / "market"
