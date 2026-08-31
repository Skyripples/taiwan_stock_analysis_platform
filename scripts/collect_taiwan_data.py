"""Unified unattended entry point for Taiwan local Data Lake collectors."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.collectors.adapters import COLLECTORS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collector", choices=sorted(COLLECTORS))
    args, forwarded = parser.parse_known_args(argv)
    return COLLECTORS[args.collector](forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
