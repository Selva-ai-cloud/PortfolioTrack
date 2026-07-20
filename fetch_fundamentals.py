"""
Fundamentals fetcher — run weekly (fundamentals only change quarterly).

Unlike prices, yfinance has no batch API for fundamentals: `.info` is ONE
HTTP request per ticker, and ROCE needs two more (income statement +
balance sheet). For ~74 symbols that is ~220 requests, so this runs as its
own slow, throttled job and writes fundamentals_cache.json for the
dashboard to read instantly.

Stores RAW metrics only — quality scoring lives in portfolio_app.py so the
thresholds can be tuned without re-fetching.

Run manually:  python3 fetch_fundamentals.py
"""
import json
import os
import sys
import time
from datetime import datetime

import yfinance as yf

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

HOLDINGS_FILE  = os.path.join(BASE_DIR, "holdings.json")
WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.json")
CACHE_FILE     = os.path.join(BASE_DIR, "fundamentals_cache.json")

THROTTLE = 0.4          # seconds between tickers — keeps Yahoo from rate-limiting

# Sectors where Current Ratio / Debt-to-Equity don't carry their usual meaning
# (leverage IS the business model for a lender).
FINANCIAL_SECTORS = {"Financial Services", "Financial", "Banks"}


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_symbols():
    """Yahoo symbols for holdings (keyed by stock name) + watchlist."""
    out = {}
    try:
        with open(HOLDINGS_FILE) as f:
            for stock, info in json.load(f).items():
                if info.get("yahoo"):
                    out[info["yahoo"]] = {"key": stock, "kind": "H"}
    except Exception as e:
        log(f"holdings load failed: {e}")
    try:
        with open(WATCHLIST_FILE) as f:
            for sym in json.load(f):
                out.setdefault(sym, {"key": sym, "kind": "W"})
    except Exception as e:
        log(f"watchlist load failed: {e}")
    return out


def compute_roce(ticker):
    """ROCE = EBIT / (Total Assets - Current Liabilities). Two extra API
    calls, so failures are tolerated and simply yield None."""
    try:
        inc = ticker.income_stmt
        bal = ticker.balance_sheet
        if inc is None or bal is None or inc.empty or bal.empty:
            return None
        if "EBIT" not in inc.index:
            return None
        ebit = float(inc.loc["EBIT"].iloc[0])
        if "Total Assets" not in bal.index or "Current Liabilities" not in bal.index:
            return None
        ta = float(bal.loc["Total Assets"].iloc[0])
        cl = float(bal.loc["Current Liabilities"].iloc[0])
        cap = ta - cl
        if cap <= 0:
            return None
        return round(ebit / cap * 100, 1)
    except Exception:
        return None


def pct(v):
    """yfinance returns ratios as decimals (0.179) — store as percent."""
    return round(float(v) * 100, 1) if v is not None else None


def fetch_one(yahoo_sym, with_roce=True):
    t    = yf.Ticker(yahoo_sym)
    info = t.info or {}
    if not info.get("symbol") and not info.get("shortName"):
        raise ValueError("empty info")

    sector      = info.get("sector")
    is_financial = sector in FINANCIAL_SECTORS

    pe     = info.get("trailingPE")
    growth = info.get("earningsGrowth")     # decimal, e.g. 0.214
    # PEG isn't provided for Indian tickers — derive it. Only meaningful
    # when growth is positive; negative growth makes a low PEG misleading.
    peg = None
    if pe and growth and growth > 0:
        peg = round(pe / (growth * 100), 2)

    de = info.get("debtToEquity")            # yfinance returns PERCENT (314.8 = 3.15x)

    return {
        "name":          info.get("shortName"),
        "sector":        sector,
        "is_financial":  is_financial,
        "pe":            round(float(pe), 1) if pe else None,
        "forward_pe":    round(float(info["forwardPE"]), 1) if info.get("forwardPE") else None,
        "peg":           peg,
        "pb":            round(float(info["priceToBook"]), 2) if info.get("priceToBook") else None,
        "current_ratio": round(float(info["currentRatio"]), 2) if info.get("currentRatio") else None,
        "de":            round(float(de) / 100, 2) if de is not None else None,
        "roe":           pct(info.get("returnOnEquity")),
        "roce":          compute_roce(t) if with_roce else None,
        "sales_growth":  pct(info.get("revenueGrowth")),
        "profit_growth": pct(growth),
    }


def main():
    with_roce = "--no-roce" not in sys.argv
    symbols = load_symbols()
    if not symbols:
        log("no symbols found — aborting")
        sys.exit(1)

    log(f"── Fundamentals fetch started — {len(symbols)} symbols "
        f"(ROCE {'on' if with_roce else 'off'}) ──")
    data, ok, fail = {}, 0, 0

    for i, (sym, meta) in enumerate(symbols.items(), 1):
        try:
            rec = fetch_one(sym, with_roce)
            rec["yahoo"] = sym
            rec["kind"]  = meta["kind"]
            data[meta["key"]] = rec
            ok += 1
            log(f"  ✅ {meta['key']:<16} P/E {str(rec['pe']):>6}  ROE {str(rec['roe']):>6}  "
                f"ROCE {str(rec['roce']):>6}  [{i}/{len(symbols)}]")
        except Exception as e:
            fail += 1
            log(f"  ⚠️  {meta['key']:<16} {type(e).__name__}: {e}  [{i}/{len(symbols)}]")
        time.sleep(THROTTLE)

    if not data:
        log("all fetches failed — cache left untouched")
        sys.exit(1)

    with open(CACHE_FILE, "w") as f:
        json.dump({"fetched_at": datetime.now().isoformat(), "data": data}, f, indent=2)
    log(f"── done: {ok} ok, {fail} failed → {CACHE_FILE} ──")


if __name__ == "__main__":
    main()
