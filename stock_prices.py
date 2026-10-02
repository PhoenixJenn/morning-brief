#!/usr/bin/env python3
"""
stock_prices.py — Tracks daily closing prices for publicly-traded companies
in the watchlist, so category-level story volume (category_trends.py) can be
compared against category-level stock movement over the same weeks.

Ticker mapping lives in context/watchlist_meta.json under each company's
"ticker"/"yf_symbol"/"exchange" fields — populated via a one-time LLM lookup
pass verified by Jenn (see project_stock_trends memory), not auto-trusted.
New companies do NOT get a ticker automatically; add one to watchlist_meta.json
manually (or via a future verified lookup pass) to start tracking it.

Runs after each brief (called from morning_brief.py), or standalone:
  python stock_prices.py                # pull today's close for all tickers
  python stock_prices.py --backfill     # full history from START_DATE to today
"""

import json
import sys
from datetime import date, datetime
from pathlib import Path

import yfinance as yf

PROJECT_DIR  = Path(__file__).parent
CONTEXT_DIR  = PROJECT_DIR.parent / "claude_projects" / "context"
META_FILE    = CONTEXT_DIR / "watchlist_meta.json"
PRICES_FILE  = CONTEXT_DIR / "stock-prices.json"

START_DATE = "2026-05-17"  # matches the earliest Morning Brief transcript


def load_tickers() -> dict:
    """Returns {company_name: yf_symbol} for every entity with a ticker."""
    if not META_FILE.exists():
        return {}
    meta = json.loads(META_FILE.read_text())
    return {name: m["yf_symbol"] for name, m in meta.items() if m.get("yf_symbol")}


def load_prices() -> dict:
    if not PRICES_FILE.exists():
        return {}
    try:
        return json.loads(PRICES_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_prices(prices: dict):
    PRICES_FILE.write_text(json.dumps(prices, indent=2, ensure_ascii=False, sort_keys=True))


def backfill():
    """Pull full daily-close history for every ticker, from START_DATE to today."""
    tickers = load_tickers()
    prices = load_prices()
    today = date.today().isoformat()

    print(f"Backfilling {len(tickers)} ticker(s) from {START_DATE} to {today}...\n")
    failed = []
    for name, symbol in tickers.items():
        try:
            hist = yf.Ticker(symbol).history(start=START_DATE, end=today, interval="1d")
            if hist.empty:
                print(f"  ⚠ {name} ({symbol}): no data returned")
                failed.append(name)
                continue
            series = {}
            for ts, row in hist.iterrows():
                series[ts.strftime("%Y-%m-%d")] = round(float(row["Close"]), 4)
            prices[symbol] = series
            print(f"  ✓ {name} ({symbol}): {len(series)} trading days")
        except Exception as exc:
            print(f"  ✗ {name} ({symbol}): {exc}")
            failed.append(name)

    save_prices(prices)
    print(f"\n✓ Backfill complete — {len(tickers) - len(failed)}/{len(tickers)} tickers recorded")
    if failed:
        print(f"  Failed: {', '.join(failed)}")


def run(date_str: str = None, verbose: bool = True) -> bool:
    """Record today's (or a given day's) close for every tracked ticker."""
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    tickers = load_tickers()
    if not tickers:
        if verbose:
            print("  [stocks] No tickers configured in watchlist_meta.json — skipping")
        return False

    prices = load_prices()
    recorded = 0
    for name, symbol in tickers.items():
        try:
            hist = yf.Ticker(symbol).history(period="5d", interval="1d")
            if hist.empty:
                continue
            last_ts, last_row = hist.iloc[-1].name, hist.iloc[-1]
            prices.setdefault(symbol, {})[last_ts.strftime("%Y-%m-%d")] = round(float(last_row["Close"]), 4)
            recorded += 1
        except Exception as exc:
            if verbose:
                print(f"  [stocks] {name} ({symbol}) failed: {exc}")

    save_prices(prices)
    if verbose:
        print(f"  [stocks] Recorded {recorded}/{len(tickers)} ticker prices → {PRICES_FILE.name}")
    return True


if __name__ == "__main__":
    if "--backfill" in sys.argv:
        backfill()
    else:
        run()
