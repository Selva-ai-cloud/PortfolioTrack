"""
Portfolio EOD Fetcher — uses yfinance (free, no API key needed).
Reads holdings from holdings.json, writes prices to portfolio_history.json.

Run manually:   python3 fetch_eod.py
Auto-run:       Claude Routine fires this at 3:35 PM Mon–Fri
"""

import os, json, time
from datetime import datetime
import yfinance as yf

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
HOLDINGS_FILE = os.path.join(BASE_DIR, "holdings.json")
HISTORY_FILE  = os.path.join(BASE_DIR, "portfolio_history.json")
LOG_FILE      = os.path.join(BASE_DIR, "portfolio_log.txt")

# ─── LOAD HOLDINGS ────────────────────────────────────────────────────────────
def load_holdings():
    with open(HOLDINGS_FILE) as f:
        return json.load(f)

HOLDINGS = load_holdings()

# ─── LOGGING ──────────────────────────────────────────────────────────────────
def log(msg):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ─── FETCH ALL PRICES VIA YFINANCE (BATCH) ───────────────────────────────────
def fetch_all_prices():
    """Fetch closing prices for all holdings using yfinance batch download."""
    # Reload holdings fresh (in case they were edited)
    holdings = load_holdings()
    prices   = {}

    symbol_map = {}
    for stock, info in holdings.items():
        if info.get("yahoo"):
            symbol_map[info["yahoo"]] = stock
        else:
            log(f"  ⏭  {stock:<18} skipped (no Yahoo symbol)")
            prices[stock] = None

    yahoo_symbols = list(symbol_map.keys())
    log(f"  📡  Batch downloading {len(yahoo_symbols)} symbols via yfinance…")

    try:
        raw = yf.download(
            tickers=yahoo_symbols,
            period="5d",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        close_df = raw["Close"] if "Close" in raw.columns else raw.xs("Close", axis=1, level=0)

        done = 0
        for yahoo_sym in yahoo_symbols:
            stock = symbol_map[yahoo_sym]
            try:
                series = close_df[yahoo_sym].dropna()
                if len(series) == 0:
                    raise ValueError("empty series")
                close = round(float(series.iloc[-1]), 2)
                prices[stock] = close
                done += 1
                log(f"  ✅  {stock:<18}  ₹{close:>10.2f}  [{done}/{len(yahoo_symbols)}]")
            except Exception as e:
                prices[stock] = None
                log(f"  ❌  {stock:<18}  could not fetch ({e})")

    except Exception as e:
        log(f"  ⚠️  Batch download failed: {e} — falling back to individual fetches…")
        for yahoo_sym, stock in symbol_map.items():
            if stock in prices:
                continue
            try:
                ticker = yf.Ticker(yahoo_sym)
                hist   = ticker.history(period="5d", interval="1d", auto_adjust=True)
                if hist.empty:
                    raise ValueError("empty history")
                close = round(float(hist["Close"].dropna().iloc[-1]), 2)
                prices[stock] = close
                log(f"  ✅  {stock:<18}  ₹{close:>10.2f}")
            except Exception as e2:
                prices[stock] = None
                log(f"  ❌  {stock:<18}  could not fetch ({e2})")
            time.sleep(0.5)

    return prices, holdings

# ─── P&L HELPERS ─────────────────────────────────────────────────────────────
def pnl_rs(avg, qty, close):
    return round((close - avg) * qty, 2)

def pnl_pct(avg, close):
    if avg == 0:
        return None
    return round(((close - avg) / avg) * 100, 2)

# ─── SAVE HISTORY JSON ────────────────────────────────────────────────────────
def save_history(date_label, prices, holdings):
    """Append/overwrite today's data in portfolio_history.json."""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            history = json.load(f)
    else:
        history = {}

    day_data   = {}
    total_pnl  = 0.0

    for stock, info in holdings.items():
        close = prices.get(stock)
        if close is not None:
            pr  = pnl_rs(info["avg"], info["qty"], close)
            pp  = pnl_pct(info["avg"], close)
            total_pnl += pr
            day_data[stock] = {"close": close, "pnl_rs": pr, "pnl_pct": pp}
        else:
            day_data[stock] = {"close": None, "pnl_rs": None, "pnl_pct": None}

    day_data["_total_pnl"] = round(total_pnl, 2)
    history[date_label] = day_data

    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

    return round(total_pnl, 2)

# ─── macOS NOTIFICATION ───────────────────────────────────────────────────────
def notify(title, msg):
    os.system(f'osascript -e \'display notification "{msg}" with title "{title}"\'')

# ─── JSON SUMMARY (for Claude Routine output) ─────────────────────────────────
def summary_json(date_label, prices, holdings):
    rows, total = [], 0.0
    for stock in sorted(holdings.keys()):
        info  = holdings[stock]
        close = prices.get(stock)
        if close:
            pr = pnl_rs(info["avg"], info["qty"], close)
            pp = pnl_pct(info["avg"], close)
            total += pr
            rows.append({"stock": stock, "close": close, "pnl_rs": pr, "pnl_pct": pp})
        else:
            rows.append({"stock": stock, "close": None, "pnl_rs": None, "pnl_pct": None})
    return {"date": date_label, "rows": rows, "total_pnl": round(total, 2)}

# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    date_label = datetime.now().strftime("%d-%b-%y")
    log(f"══ EOD fetch started ({date_label}) ══")

    prices, holdings = fetch_all_prices()
    total            = save_history(date_label, prices, holdings)
    summary          = summary_json(date_label, prices, holdings)

    print("\n__SUMMARY_JSON__")
    print(json.dumps(summary, indent=2))
    print("__SUMMARY_JSON_END__")

    notify("Portfolio Updated ✅",
           f"EOD {date_label} | Net P&L: {'+'if total>=0 else ''}₹{total:,.0f}")
    log(f"══ Done. Net P&L = ₹{total:,.2f} — saved to {HISTORY_FILE} ══\n")
