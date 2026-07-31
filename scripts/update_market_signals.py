"""Generate the platform's unified market signal output."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from analysis.market_signal_engine import MarketSignalEngine
from config import MARKET_DATA_DIR


LOGGER = logging.getLogger("market_analysis")
OUTPUT_PATH = MARKET_DATA_DIR / "market_signals.json"


def write_json(output_path: Path, payload: object) -> None:
    """Write JSON atomically so failed runs preserve the previous output."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as output_file:
            json.dump(payload, output_file, ensure_ascii=False, indent=2)
            output_file.write("\n")
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    engine = MarketSignalEngine(MARKET_DATA_DIR)
    LOGGER.info("Market analysis started: %s", engine.analysis_name)

    try:
        source_data = engine.load()
        signals = engine.analyze(source_data)
        payload = engine.export(signals)
        write_json(OUTPUT_PATH, payload)
    except (OSError, ValueError) as exc:
        LOGGER.error("Market analysis failed: %s", exc)
        return 1

    LOGGER.info("Market signals written: %s", OUTPUT_PATH)
    LOGGER.info("Market analysis finished | signals=%d", len(signals))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
