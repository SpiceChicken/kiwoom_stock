#!/usr/bin/env python3
"""Compatibility wrapper for the installed live read-only validator."""

from kiwoom_stock.validation.live_readonly import main


if __name__ == "__main__":
    raise SystemExit(main())
