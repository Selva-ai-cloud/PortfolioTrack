"""
Portfolio Web Dashboard
Run:  python3 portfolio_app.py
Open: http://localhost:5050
"""

import json
import os
import subprocess
from datetime import datetime, timedelta

import yfinance as yf
from flask import Flask, jsonify, redirect, render_template_string, request, url_for

BASE_DIR            = os.path.dirname(os.path.abspath(__file__))
HOLDINGS_FILE       = os.path.join(BASE_DIR, "holdings.json")
HISTORY_FILE        = os.path.join(BASE_DIR, "portfolio_history.json")
DMA_CACHE_FILE      = os.path.join(BASE_DIR, "dma_cache.json")
BENCH_CACHE_FILE    = os.path.join(BASE_DIR, "benchmark_cache.json")
WATCHLIST_FILE      = os.path.join(BASE_DIR, "watchlist.json")
WL_DMA_CACHE_FILE   = os.path.join(BASE_DIR, "watchlist_dma_cache.json")
FETCH_SCRIPT        = os.path.join(BASE_DIR, "fetch_eod.py")
PYTHON = "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"

app = Flask(__name__)


# ── Data helpers ──────────────────────────────────────────────────────────────
def load_holdings():
    with open(HOLDINGS_FILE) as f:
        return json.load(f)


def save_holdings(h):
    with open(HOLDINGS_FILE, "w") as f:
        json.dump(h, f, indent=2)


def load_history():
    """Load history JSON; return {} on any parse error so the dashboard never crashes."""
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE) as f:
            raw = json.load(f)
        # Normalise pnl_pct: old data stored it as a decimal fraction (0.0772).
        # New fetch_eod.py stores plain percent (-9.41).  If |value| < 2 for most
        # stocks we are almost certainly looking at the old fractional format.
        for date_key, day in raw.items():
            values = [
                v.get("pnl_pct")
                for v in day.values()
                if isinstance(v, dict) and v.get("pnl_pct") is not None
            ]
            if values and all(abs(v) < 2 for v in values):
                # Looks like decimal fractions — multiply by 100
                for stock_data in day.values():
                    if (
                        isinstance(stock_data, dict)
                        and stock_data.get("pnl_pct") is not None
                    ):
                        stock_data["pnl_pct"] = round(stock_data["pnl_pct"] * 100, 4)
        return raw
    except Exception:
        # Corrupted file — back it up and start fresh
        import shutil
        import time

        backup = HISTORY_FILE + f".bak.{int(time.time())}"
        shutil.copy2(HISTORY_FILE, backup)
        return {}


# ── DMA helpers ───────────────────────────────────────────────────────────────
def _detect_cross(cmp, prev_close, dma_today, dma_prev, label):
    """Return a cross description if price crossed the DMA vs previous day."""
    if dma_today is None or dma_prev is None:
        return None
    was_below = prev_close < dma_prev
    now_above = cmp >= dma_today
    was_above = prev_close > dma_prev
    now_below = cmp <= dma_today
    if was_below and now_above:
        return f"↑ {label}"
    if was_above and now_below:
        return f"↓ {label}"
    return None


def _signal(cmp, ema20, dma50, dma200, dma200_slope, ema_dist_pct):
    """
    Strategy signal using 200DMA (trend), 50DMA (strength), 20EMA (entry timing).
      Avoid      — price below 200DMA (bear territory)
      Caution    — trend broken: 50DMA < 200DMA or price well below 20EMA
      Watch      — trend bullish but price far above 20EMA; wait for pullback
      Near Entry — trend bullish, price pulling back toward 20EMA (2–8% above)
      Buy Setup  — trend confirmed + price at/near 20EMA bounce zone
    """
    if any(v is None for v in [cmp, ema20, dma50, dma200]):
        return "—"
    if cmp < dma200:
        return "Avoid"
    if dma50 < dma200:
        return "Caution"
    # Trend: both 50DMA > 200DMA (already checked) + 200DMA sloping up
    trend_confirmed = (dma200_slope == "up")
    if ema_dist_pct is None:
        return "Watch"
    # Price has broken down well below 20EMA
    if ema_dist_pct < -3:
        return "Caution"
    # At/near 20EMA — prime entry zone
    if -3 <= ema_dist_pct <= 2:
        return "Buy Setup" if trend_confirmed else "Near Entry"
    # Pulling back toward EMA (2–8% above it)
    if 2 < ema_dist_pct <= 8:
        return "Near Entry" if trend_confirmed else "Watch"
    # Far above EMA (>8%) — wait for pullback
    return "Watch"


def load_watchlist():
    """Return list of Yahoo Finance symbols in watchlist."""
    if not os.path.exists(WATCHLIST_FILE):
        return []
    try:
        with open(WATCHLIST_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_watchlist(wl):
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(wl, f, indent=2)


def _compute_dma_for_symbols(symbol_map):
    """
    Core DMA/EMA computation.
    symbol_map: {yahoo_symbol: display_key}
    Returns: {display_key: {...metrics...}}
    """
    if not symbol_map:
        return {}
    yahoo_symbols = list(symbol_map.keys())

    try:
        raw = yf.download(
            tickers=yahoo_symbols,
            period="1y",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        # yfinance always returns multi-level columns (even single ticker)
        try:
            close_df = raw.xs("Close", axis=1, level=0)
        except (KeyError, TypeError):
            close_df = raw["Close"] if "Close" in raw.columns else raw
        try:
            vol_df = raw.xs("Volume", axis=1, level=0)
        except (KeyError, TypeError):
            vol_df = None
    except Exception as e:
        return {"error": str(e)}

    result = {}
    for yahoo_sym in yahoo_symbols:
        key = symbol_map[yahoo_sym]
        try:
            series = close_df[yahoo_sym].dropna() if yahoo_sym in close_df.columns else close_df.dropna()
            if len(series) < 2:
                raise ValueError("insufficient data")

            cmp        = round(float(series.iloc[-1]), 2)
            prev_close = round(float(series.iloc[-2]), 2)

            ema_series = series.ewm(span=20, adjust=False).mean()
            ema20      = round(float(ema_series.iloc[-1]), 2)
            prev_ema20 = round(float(ema_series.iloc[-2]), 2)

            dma50  = round(float(series.iloc[-50:].mean()),  2) if len(series) >= 50  else None
            dma200 = round(float(series.iloc[-200:].mean()), 2) if len(series) >= 200 else None

            prev_dma50  = round(float(series.iloc[-51:-1].mean()), 2) if len(series) >= 51  else None
            prev_dma200 = round(float(series.iloc[-201:-1].mean()),2) if len(series) >= 201 else None

            dma200_slope = None
            if len(series) >= 220:
                d200_now = float(series.iloc[-200:].mean())
                d200_ago = float(series.iloc[-220:-20].mean())
                dma200_slope = "up" if d200_now > d200_ago * 1.001 else (
                               "down" if d200_now < d200_ago * 0.999 else "flat")

            ema_dist_pct = round((cmp - ema20) / ema20 * 100, 2) if ema20 else None

            # Volume vs 20-day average — breakout confirmation. NOTE: during
            # market hours today's bar is partial, so the ratio underestimates.
            vol_ratio = None
            if vol_df is not None and yahoo_sym in getattr(vol_df, "columns", []):
                vseries = vol_df[yahoo_sym].reindex(series.index).dropna()
                if len(vseries) >= 21:
                    avg20 = float(vseries.iloc[-21:-1].mean())
                    if avg20 > 0:
                        vol_ratio = round(float(vseries.iloc[-1]) / avg20, 2)

            # Distance below the 1-year high — trailing-drawdown proxy for exits
            high_1y = float(series.max())
            off_high_pct = round((cmp - high_1y) / high_1y * 100, 1) if high_1y else None

            # RSI(14) — Wilder smoothing (same series, no extra API call)
            rsi14 = None
            if len(series) >= 15:
                delta    = series.diff()
                avg_gain = delta.clip(lower=0).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
                avg_loss = (-delta.clip(upper=0)).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
                rs       = avg_gain / avg_loss.replace(0, float('nan'))
                rsi_ser  = 100 - (100 / (1 + rs))
                rsi14    = round(float(rsi_ser.iloc[-1]), 1) if not rsi_ser.empty else None

            crosses = []
            for ma_t, ma_p, lbl in [
                (dma200, prev_dma200, "200DMA"),
                (dma50,  prev_dma50,  "50DMA"),
                (ema20,  prev_ema20,  "20EMA"),
            ]:
                c = _detect_cross(cmp, prev_close, ma_t, ma_p, lbl)
                if c:
                    crosses.append(c)

            result[key] = {
                "cmp":          cmp,
                "ema20":        ema20,
                "dma50":        dma50,
                "dma200":       dma200,
                "dma200_slope": dma200_slope,
                "ema_dist_pct": ema_dist_pct,
                "rsi14":        rsi14,
                "vol_ratio":    vol_ratio,
                "off_high_pct": off_high_pct,
                "cross":        ", ".join(crosses) if crosses else "—",
                "signal":       _signal(cmp, ema20, dma50, dma200, dma200_slope, ema_dist_pct),
                "stop":         dma50,
            }
        except Exception:
            result[key] = {
                "cmp": None, "ema20": None, "dma50": None, "dma200": None,
                "dma200_slope": None, "ema_dist_pct": None, "rsi14": None,
                "vol_ratio": None, "off_high_pct": None,
                "cross": "—", "signal": "—", "stop": None,
            }
    return result


def compute_dma_data():
    """Holdings DMA — keyed by portfolio stock name."""
    holdings = load_holdings()
    symbol_map = {info["yahoo"]: stock
                  for stock, info in holdings.items() if info.get("yahoo")}
    return _compute_dma_for_symbols(symbol_map)


def compute_watchlist_dma():
    """Watchlist DMA — keyed by Yahoo symbol (display name = symbol prefix)."""
    wl = load_watchlist()
    if not wl:
        return {}
    # key = Yahoo symbol itself so the JS can match it back
    symbol_map = {sym: sym for sym in wl}
    return _compute_dma_for_symbols(symbol_map)


def _dma_result_ok(data):
    """
    True only if `data` is a usable result set — not an error payload and not
    an all-empty fetch. Used to avoid caching (or serving) a transient yfinance
    failure for the full 4-hour TTL, which would leave the Technical tab and
    Signal Scanner blank long after Yahoo has recovered.
    """
    if not isinstance(data, dict) or not data:
        return False
    if "error" in data and len(data) == 1:        # {"error": "..."} from download failure
        return False
    # require at least one symbol with a real CMP
    return any(isinstance(v, dict) and v.get("cmp") is not None for v in data.values())


# ── DMA API route ──────────────────────────────────────────────────────────────
@app.route("/api/dma")
def api_dma():
    force = request.args.get("force", "0") == "1"
    # Use cache if fresh (< 4 hours)
    if not force and os.path.exists(DMA_CACHE_FILE):
        try:
            with open(DMA_CACHE_FILE) as f:
                cache = json.load(f)
            cached_at = datetime.fromisoformat(cache.get("fetched_at", "2000-01-01"))
            if datetime.now() - cached_at < timedelta(hours=4) and _dma_result_ok(cache.get("data")):
                return jsonify(cache["data"])
        except Exception:
            pass

    data = compute_dma_data()
    if _dma_result_ok(data):                      # never cache a failure / empty fetch
        try:
            with open(DMA_CACHE_FILE, "w") as f:
                json.dump({"fetched_at": datetime.now().isoformat(), "data": data}, f, indent=2)
        except Exception:
            pass
    return jsonify(data)


# ── Watchlist DMA API ─────────────────────────────────────────────────────────
@app.route("/api/watchlist-dma")
def api_watchlist_dma():
    force = request.args.get("force", "0") == "1"
    if not force and os.path.exists(WL_DMA_CACHE_FILE):
        try:
            with open(WL_DMA_CACHE_FILE) as f:
                cache = json.load(f)
            cached_at = datetime.fromisoformat(cache.get("fetched_at", "2000-01-01"))
            if datetime.now() - cached_at < timedelta(hours=4) and _dma_result_ok(cache.get("data")):
                return jsonify({"data": cache["data"], "symbols": load_watchlist()})
        except Exception:
            pass
    data = compute_watchlist_dma()
    if _dma_result_ok(data):                      # never cache a failure / empty fetch
        try:
            with open(WL_DMA_CACHE_FILE, "w") as f:
                json.dump({"fetched_at": datetime.now().isoformat(), "data": data}, f, indent=2)
        except Exception:
            pass
    return jsonify({"data": data, "symbols": load_watchlist()})


@app.route("/watchlist/add", methods=["POST"])
def watchlist_add():
    sym = request.form.get("symbol", "").strip()
    if not sym:
        return jsonify({"ok": False, "error": "empty symbol"})
    wl = load_watchlist()
    if sym not in wl:
        wl.append(sym)
        save_watchlist(wl)
    return jsonify({"ok": True, "symbols": wl})


@app.route("/watchlist/remove", methods=["POST"])
def watchlist_remove():
    sym = request.form.get("symbol", "").strip()
    wl = load_watchlist()
    if sym in wl:
        wl.remove(sym)
        save_watchlist(wl)
    return jsonify({"ok": True, "symbols": wl})


# ── Dashboard route ───────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    holdings = load_holdings()
    history = load_history()
    fetch_msg = request.args.get("fetch_msg", "")

    dates = sorted(history.keys(), key=lambda d: datetime.strptime(d, "%d-%b-%y"))
    today_data = history[dates[-1]] if dates else {}
    prev_data = history[dates[-2]] if len(dates) > 1 else {}
    last_updated = dates[-1] if dates else "No data yet"

    # Build rows
    rows = []
    total_pnl = 0.0
    for stock in sorted(holdings.keys()):
        info = holdings[stock]
        td = today_data.get(stock, {})
        close = td.get("close")
        pnl_r = td.get("pnl_rs")
        pnl_p = td.get("pnl_pct")

        prev_close = prev_data.get(stock, {}).get("close")
        if close and prev_close:
            day_chg = round((close - prev_close) / prev_close * 100, 2)
            day_chg_rs = round(close - prev_close, 2)
        else:
            day_chg = day_chg_rs = None

        if pnl_r:
            total_pnl += pnl_r

        rows.append(
            {
                "stock": stock,
                "exchange": "BSE"
                if (info.get("yahoo") or "").endswith(".BO")
                else "NSE",
                "broker": info.get("broker", "Zerodha"),
                "qty": info["qty"],
                "avg": info["avg"],
                "yahoo": info.get("yahoo") or "",
                "close": close,
                "pnl_rs": pnl_r,
                "pnl_pct": pnl_p,
                "day_chg": day_chg,
                "day_chg_rs": day_chg_rs,
                "prev_close": prev_close,
                "buy_date": info.get("buy_date"),
            }
        )

    # Top 3 gainers / losers by today's daily move
    scored = [(r["stock"], r["day_chg"]) for r in rows if r["day_chg"] is not None]
    if not scored:  # fallback: use overall pnl_pct on first day
        scored = [(r["stock"], r["pnl_pct"]) for r in rows if r["pnl_pct"] is not None]

    scored_desc = sorted(scored, key=lambda x: x[1], reverse=True)
    gainers = [
        next(r for r in rows if r["stock"] == s) | {"day_pct": p}
        for s, p in scored_desc[:3]
    ]
    losers = [
        next(r for r in rows if r["stock"] == s) | {"day_pct": p}
        for s, p in scored_desc[-3:][::-1]
    ]

    # Chart data: {date: {stock: pnl_pct}} + parallel close prices for tooltip
    chart_data  = {}
    chart_close = {}
    for d in dates:
        chart_data[d] = {
            s: v.get("pnl_pct")
            for s, v in history[d].items()
            if s != "_total_pnl" and v.get("pnl_pct") is not None
        }
        chart_close[d] = {
            s: v.get("close")
            for s, v in history[d].items()
            if s != "_total_pnl" and v.get("close") is not None
        }

    return render_template_string(
        HTML,
        rows=rows,
        total_pnl=round(total_pnl, 2),
        gainers=gainers,
        losers=losers,
        holdings=holdings,
        chart_data=chart_data,
        chart_close=chart_close,
        dates=dates,
        stocks=sorted(holdings.keys()),
        last_updated=last_updated,
        fetch_msg=fetch_msg,
        watchlist=load_watchlist(),
    )


# ── Benchmark API (NIFTY 50 for the P&L chart overlay) ───────────────────────
@app.route("/api/benchmark")
def api_benchmark():
    """1y of NIFTY 50 daily closes keyed by chart date label; 4h cache."""
    if os.path.exists(BENCH_CACHE_FILE):
        try:
            with open(BENCH_CACHE_FILE) as f:
                cache = json.load(f)
            cached_at = datetime.fromisoformat(cache.get("fetched_at", "2000-01-01"))
            if datetime.now() - cached_at < timedelta(hours=4) and cache.get("data"):
                return jsonify(cache["data"])
        except Exception:
            pass
    try:
        raw = yf.download(tickers="^NSEI", period="1y", interval="1d",
                          auto_adjust=True, progress=False)
        try:
            close = raw.xs("Close", axis=1, level=0)
            series = close[close.columns[0]].dropna()
        except (KeyError, TypeError, AttributeError):
            series = raw["Close"].dropna()
        # key format matches portfolio_history / chart labels: 17-Jun-26
        data = {ts.strftime("%d-%b-%y"): round(float(v), 2)
                for ts, v in series.items()}
    except Exception as e:
        return jsonify({"error": str(e)})
    if data:                                      # never cache a failure
        try:
            with open(BENCH_CACHE_FILE, "w") as f:
                json.dump({"fetched_at": datetime.now().isoformat(), "data": data}, f)
        except Exception:
            pass
    return jsonify(data)


# ── Stock CRUD ────────────────────────────────────────────────────────────────
@app.route("/add-stock", methods=["POST"])
def add_stock():
    h = load_holdings()
    symbol = request.form["symbol"].strip().upper()
    yahoo = request.form["yahoo"].strip() or None
    avg = float(request.form["avg"])
    qty = int(request.form["qty"])
    broker = request.form.get("broker", "Zerodha")
    buy_date = request.form.get("buy_date", "").strip() or None
    h[symbol] = {"yahoo": yahoo, "avg": avg, "qty": qty, "broker": broker,
                 "buy_date": buy_date}
    save_holdings(h)
    return redirect(url_for("dashboard"))


@app.route("/update-stock", methods=["POST"])
def update_stock():
    h = load_holdings()
    symbol = request.form["symbol"].strip().upper()
    if symbol in h:
        h[symbol]["qty"] = int(request.form["qty"])
        h[symbol]["avg"] = float(request.form["avg"])
        h[symbol]["yahoo"] = request.form["yahoo"].strip() or None
        h[symbol]["broker"] = request.form.get("broker", "Zerodha")
        h[symbol]["buy_date"] = request.form.get("buy_date", "").strip() or None
    save_holdings(h)
    return redirect(url_for("dashboard"))


@app.route("/add-purchase", methods=["POST"])
def add_purchase():
    """Average an additional purchase into an existing holding:
    qty adds up, avg becomes the weighted mean of old and new lots."""
    h = load_holdings()
    symbol = request.form["symbol"].strip().upper()
    try:
        add_qty   = int(request.form["add_qty"])
        add_price = float(request.form["add_price"])
    except (KeyError, ValueError):
        return redirect(url_for("dashboard"))
    if symbol in h and add_qty > 0 and add_price > 0:
        old_qty = h[symbol]["qty"]
        old_avg = h[symbol]["avg"]
        new_qty = old_qty + add_qty
        h[symbol]["qty"] = new_qty
        h[symbol]["avg"] = round((old_avg * old_qty + add_price * add_qty) / new_qty, 6)
        # buy_date stays as the FIRST purchase date — the LTCG timer is
        # conservative for averaged lots (real tax is FIFO per lot).
        save_holdings(h)
    return redirect(url_for("dashboard"))


@app.route("/delete-stock", methods=["POST"])
def delete_stock():
    h = load_holdings()
    symbol = request.form["symbol"].strip().upper()
    h.pop(symbol, None)
    save_holdings(h)
    return redirect(url_for("dashboard"))


@app.route("/fetch-now", methods=["POST"])
def fetch_now():
    subprocess.Popen([PYTHON, FETCH_SCRIPT])
    return redirect(url_for("dashboard", fetch_msg="fetch_started"))


# ── HTML template ─────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio Tracker</title>
<!-- Inter variable font (brand typeface per design system) -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@100..900&display=swap" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js"></script>
<style>
  /* ── Brand typeface ── */
  body{background:#f0f2f5;font-family:'Inter','Segoe UI',system-ui,-apple-system,'Helvetica Neue',Arial,sans-serif;
       font-size:0.88rem;font-feature-settings:'cv11','ss01'}

  /* ── Topbar ── */
  .topbar{background:#1F3864;color:#fff;padding:10px 20px;display:flex;align-items:center;gap:12px;
          box-shadow:0 2px 6px rgba(0,0,0,.3);flex-wrap:wrap}
  .topbar .brand{font-size:1.1rem;font-weight:700;letter-spacing:.5px}
  .topbar .updated{opacity:.6;font-size:.8rem;flex:1;min-width:120px}

  /* ── Cards ── */
  .card{border:none;border-radius:10px;box-shadow:0 1px 6px rgba(0,0,0,.09)}
  .card-hdr      {background:#1F3864;color:#fff;font-weight:600;padding:10px 16px;border-radius:10px 10px 0 0;font-size:.9rem}
  .card-hdr-green{background:#567044;color:#fff;font-weight:600;padding:10px 16px;border-radius:10px 10px 0 0}
  .card-hdr-red  {background:#e06235;color:#fff;font-weight:600;padding:10px 16px;border-radius:10px 10px 0 0}

  /* ── Hero ── */
  .hero{background:linear-gradient(135deg,#1F3864 0%,#2d5ca8 100%);color:#fff;border-radius:12px;
        padding:18px 22px;box-shadow:0 3px 12px rgba(0,0,0,.2)}
  .hero .label{font-size:.78rem;opacity:.7;text-transform:uppercase;letter-spacing:.8px}
  /* ── Hero broker filter ── */
  .hero-filter{display:flex;align-items:center;gap:6px;margin-bottom:12px;flex-wrap:wrap}
  .hero-filter .flabel{font-size:.75rem;opacity:.65;margin-right:2px;white-space:nowrap}
  .btn-hfilter{padding:3px 13px;font-size:.75rem;border-radius:16px;
               border:1.5px solid rgba(255,255,255,.45);background:transparent;
               color:rgba(255,255,255,.75);cursor:pointer;transition:all .15s;white-space:nowrap}
  .btn-hfilter:hover{background:rgba(255,255,255,.12)}
  .btn-hfilter.active{background:rgba(255,255,255,.22);border-color:#fff;color:#fff;font-weight:600}
  .btn-hfilter.gw.active{background:rgba(86,112,68,.65);border-color:#a8d08d}
  /* ── Hero summary table ── */
  .hero-tbl{width:100%;border-collapse:collapse;font-size:.82rem}
  .hero-tbl th{opacity:.6;font-weight:500;font-size:.7rem;text-transform:uppercase;
               letter-spacing:.6px;padding:2px 8px 6px;text-align:right;white-space:nowrap}
  .hero-tbl th:first-child{text-align:left}
  .hero-tbl td{padding:5px 8px;text-align:right;font-variant-numeric:tabular-nums;
               font-feature-settings:'tnum';font-weight:600;white-space:nowrap}
  .hero-tbl td:first-child{text-align:left;font-weight:700}
  .hero-tbl tbody tr{border-top:1px solid rgba(255,255,255,.1)}
  .hero-tbl .hero-total td{border-top:2px solid rgba(255,255,255,.3)!important;
                            font-size:.85rem;opacity:.95}
  .hpos{color:#C6EFCE}  .hneg{color:#ffb3b3}  .hneu{color:#FFE57F}

  /* ── Status colors (Excel conditional-format palette) ── */
  .pos{color:#1c8c44;font-weight:600}  .neg{color:#c0392b;font-weight:600}  .neu{color:#8a6800;font-weight:600}
  .pos-bg{background:#C6EFCE!important}  .neg-bg{background:#FFC7CE!important}  .neu-bg{background:#FFEB9C!important}

  /* ── Tables — tabular-nums on all cells for column alignment ── */
  table thead th{background:#1F3864;color:#fff;border:none;white-space:nowrap;font-weight:500;
                 position:sticky;top:0;z-index:2;padding:8px 10px;
                 font-variant-numeric:tabular-nums;font-feature-settings:'tnum'}
  table td{vertical-align:middle;padding:6px 10px;border-color:#e8e8e8;
           font-variant-numeric:tabular-nums;font-feature-settings:'tnum'}
  .tbl-wrap{max-height:480px;overflow-y:auto;overflow-x:auto;border-radius:0 0 10px 10px}
  .total-row td{background:#1F3864!important;color:#fff!important;font-weight:700}

  /* ── Exchange chips ── */
  .badge-bse{background:#17375e;color:#fff;font-size:.7rem;padding:2px 6px;border-radius:4px}
  .badge-nse{background:#1a5276;color:#fff;font-size:.7rem;padding:2px 6px;border-radius:4px}

  /* ── Buttons ── */
  .btn-navy{background:#1F3864;color:#fff;border:none}
  .btn-navy:hover{background:#16305a;color:#fff}
  .btn-green{background:#567044;color:#fff;border:none}
  .btn-green:hover{background:#455a37;color:#fff}

  /* ── Fetch toast ── */
  .toast-bar{position:fixed;bottom:20px;right:20px;background:#567044;color:#fff;
             padding:12px 20px;border-radius:8px;font-weight:500;
             box-shadow:0 3px 10px rgba(0,0,0,.25);z-index:9999;display:none}

  /* ── Movers sub-tables ── */
  .mover-card td{padding:7px 10px;vertical-align:middle}
  .sm-tbl thead th{background:#888;font-size:.8rem;padding:5px 8px}
  .green-tbl thead th{background:#567044}
  .red-tbl   thead th{background:#e06235}

  /* ── Chart container — clamp scales height with viewport ── */
  .chart-wrap{position:relative;height:clamp(200px,38vw,340px)}

  /* ── Right-align numeric cells (design system: components-holdings-table.html) ── */
  .r{text-align:right}

  /* ── Modal footer (design system: components-modals.html) ── */
  .modal-footer{background:#f8f9fa;border-top:1px solid #e8e8e8}

  /* ── Broker filter bar ── */
  .filter-bar{display:flex;align-items:center;gap:6px;padding:8px 16px;
              background:#eef0f4;border-bottom:1px solid #dde2ea;flex-wrap:wrap}
  .filter-bar .flabel{font-size:.78rem;font-weight:600;color:#555;margin-right:2px;white-space:nowrap}
  .btn-filter{padding:3px 14px;font-size:.78rem;border-radius:20px;border:1.5px solid #1F3864;
              background:#fff;color:#1F3864;font-weight:500;cursor:pointer;transition:all .15s;white-space:nowrap}
  .btn-filter:hover{opacity:.85}
  .btn-filter.active{background:#1F3864;color:#fff}
  .btn-filter.gw{border-color:#567044;color:#567044}
  .btn-filter.gw.active{background:#567044;color:#fff}
  .filter-bar .fcount{margin-left:auto;font-size:.75rem;color:#777}

  /* ── Sortable column headers ── */
  th.sortable{cursor:pointer;user-select:none}
  th.sortable:hover{background:#2a4a80}
  th.sortable .si{opacity:.35;font-size:.65em;margin-left:3px}
  th.sort-asc  .si::after{content:'▲';opacity:1}
  th.sort-desc .si::after{content:'▼';opacity:1}
  th.sortable:not(.sort-asc):not(.sort-desc) .si::after{content:'⇅'}

  /* ── Chart stock selector panel ── */
  .stock-sel-bar{display:flex;align-items:center;gap:8px;padding:7px 14px;
                 background:#eef0f4;border-bottom:1px solid #dde2ea;flex-wrap:wrap}
  .stock-sel-bar .slabel{font-size:.78rem;font-weight:600;color:#555;white-space:nowrap}
  .btn-sel{padding:2px 11px;font-size:.75rem;border-radius:14px;border:1.5px solid #888;
           background:#fff;color:#555;cursor:pointer;transition:all .15s}
  .btn-sel:hover{background:#eee}
  .btn-sel.dis-all{border-color:#c0392b;color:#c0392b}
  .btn-sel.en-all {border-color:#567044;color:#567044}
  #cb-toggle{font-size:.75rem;color:#1F3864;cursor:pointer;text-decoration:underline;
             margin-left:auto;white-space:nowrap}
  .cb-panel{padding:8px 14px 10px;background:#f8f9fc;border-bottom:1px solid #e2e6ea;
            display:none}
  .cb-panel.open{display:block}
  .cb-grid{display:flex;flex-wrap:wrap;gap:3px 14px;max-height:110px;overflow-y:auto;
           padding-top:4px}
  .cb-item{display:flex;align-items:center;gap:4px;font-size:.74rem;cursor:pointer;white-space:nowrap}
  .cb-item input{width:13px;height:13px;cursor:pointer}
  .cb-vis{font-size:.72rem;color:#777;margin-left:6px}

  /* ── Icon action buttons ── */
  .icon-btn{background:none;border:none;padding:3px 4px;cursor:pointer;border-radius:6px;
            display:inline-flex;align-items:center;justify-content:center;
            transition:background .15s,transform .1s;vertical-align:middle}
  .icon-btn:hover{background:rgba(0,0,0,.08);transform:scale(1.1)}
  .icon-btn:active{transform:scale(.95)}

  /* ── Holdings / Technical tab switcher ── */
  .card-tabs{display:flex;gap:0;align-items:stretch;overflow-x:auto;-webkit-overflow-scrolling:touch}
  .card-tab{padding:9px 18px;font-size:.85rem;font-weight:600;cursor:pointer;
            background:rgba(255,255,255,.12);color:rgba(255,255,255,.7);
            border:none;border-right:1px solid rgba(255,255,255,.15);
            transition:all .15s;white-space:nowrap}
  .card-tab:first-child{border-radius:10px 0 0 0}
  .card-tab.active{background:rgba(255,255,255,.25);color:#fff}
  .card-tab:hover:not(.active){background:rgba(255,255,255,.18);color:#fff}

  /* ── DMA table ── */
  .dma-above{color:#1c8c44!important;font-weight:600}
  .dma-below{color:#c0392b!important;font-weight:600}
  .dma-neutral{color:#8a6800!important}
  .badge-bull{background:#C6EFCE;color:#1c5e2e;padding:2px 8px;border-radius:10px;font-size:.75rem;font-weight:700;white-space:nowrap}
  .badge-bear{background:#FFC7CE;color:#9b1c1c;padding:2px 8px;border-radius:10px;font-size:.75rem;font-weight:700;white-space:nowrap}
  .badge-mbull{background:#e2f0d9;color:#375623;padding:2px 8px;border-radius:10px;font-size:.75rem;font-weight:600;white-space:nowrap}
  .badge-mbear{background:#fce4ec;color:#7b1034;padding:2px 8px;border-radius:10px;font-size:.75rem;font-weight:600;white-space:nowrap}
  .badge-neu{background:#FFEB9C;color:#7d6608;padding:2px 8px;border-radius:10px;font-size:.75rem;font-weight:600;white-space:nowrap}
  .cross-up{color:#1c8c44;font-weight:700}
  .cross-dn{color:#c0392b;font-weight:700}
  /* ── Signal badges ── */
  .sig-buy  {background:#1c8c44;color:#fff;padding:2px 9px;border-radius:10px;font-size:.74rem;font-weight:700;white-space:nowrap}
  .sig-near {background:#e67e22;color:#fff;padding:2px 9px;border-radius:10px;font-size:.74rem;font-weight:700;white-space:nowrap}
  .sig-watch{background:#2980b9;color:#fff;padding:2px 9px;border-radius:10px;font-size:.74rem;font-weight:600;white-space:nowrap}
  .sig-caut {background:#c0392b;color:#fff;padding:2px 9px;border-radius:10px;font-size:.74rem;font-weight:600;white-space:nowrap}
  .sig-avoid{background:#7f8c8d;color:#fff;padding:2px 9px;border-radius:10px;font-size:.74rem;font-weight:600;white-space:nowrap}
  .slope-up  {color:#1c8c44;font-weight:700}
  .slope-dn  {color:#c0392b;font-weight:700}
  .slope-flat{color:#8a6800;font-weight:600}
  .rsi-os {color:#1c8c44;font-weight:700}   /* oversold  <30  — bullish */
  .rsi-ob {color:#c0392b;font-weight:700}   /* overbought>70  — bearish */
  .rsi-neu{color:#555;font-weight:500}      /* neutral 30-70 */

  /* ── Signal Scanner card ── */
  .scanner-summary-bar{display:flex;gap:10px;padding:8px 16px;background:#eef0f4;
                       border-bottom:1px solid #dde2ea;flex-wrap:wrap;align-items:center}
  .scanner-pill{display:inline-flex;align-items:center;gap:5px;font-size:.76rem;
                padding:3px 10px;border-radius:12px;font-weight:600}
  .scanner-pill.buy {background:#d4edda;color:#1c5e2e}
  .scanner-pill.near{background:#fde8d0;color:#7d3c00}
  .scanner-pill.watch{background:#d6eaf8;color:#154360}
  /* clickable filter pills */
  .scanner-pill.clickable{cursor:pointer;user-select:none;transition:box-shadow .12s,opacity .12s}
  .scanner-pill.clickable:hover{opacity:.85}
  .scanner-pill.on{box-shadow:0 0 0 2px currentColor}
  .scanner-pill.on::after{content:'✓';font-weight:700;margin-left:2px}

  /* ── Volume ratio colors ── */
  .vol-hot{color:#1c8c44;font-weight:700}
  .vol-up {color:#1c8c44;font-weight:600}
  .vol-neu{color:#555}
  .vol-low{color:#999}

  /* ── Exit Radar reason badges ── */
  .ex-badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.72rem;
            font-weight:600;white-space:nowrap;margin:1px 3px 1px 0}
  .ex-stop  {background:#c0392b;color:#fff}
  .ex-trend {background:#7f8c8d;color:#fff}
  .ex-weak  {background:#FFC7CE;color:#9b1c1c}
  .ex-profit{background:#C6EFCE;color:#1c5e2e}

  /* ── Watchlist add bar ── */
  .wl-add-bar{display:flex;align-items:center;gap:8px;padding:8px 16px;
              background:#eef0f4;border-bottom:1px solid #dde2ea;flex-wrap:wrap}
  .wl-sym-input{font-size:.8rem;padding:4px 10px;border:1.5px solid #c0cfe0;border-radius:8px;
                width:220px;max-width:100%;outline:none;font-family:inherit}
  .wl-sym-input:focus{border-color:#1F3864;box-shadow:0 0 0 2px rgba(31,56,100,.12)}
  .wl-add-btn{font-size:.8rem;background:#567044;color:#fff;border:none;border-radius:8px;
              padding:4px 14px;cursor:pointer;font-weight:600;transition:background .15s}
  .wl-add-btn:hover{background:#455a37}
  .wl-remove-btn{background:none;border:none;cursor:pointer;color:#c0392b;font-size:.85rem;
                 padding:2px 4px;border-radius:4px;transition:background .12s}
  .wl-remove-btn:hover{background:#fce4ec}

  .dma-refresh-btn{font-size:.75rem;background:#1F3864;color:#fff;
                   border:1px solid #1F3864;border-radius:12px;padding:3px 12px;
                   cursor:pointer;transition:all .15s;white-space:nowrap;font-weight:500}
  .dma-refresh-btn:hover{background:#16305a;border-color:#16305a}
  .dma-loading{text-align:center;padding:24px;color:#777;font-size:.9rem}

  /* ── Tablet breakpoint (576–991px) ── */
  @media (max-width: 991px) {
    .hero{padding:14px 16px}
    .card-hdr,.card-hdr-green,.card-hdr-red{font-size:.85rem;padding:9px 14px}
    .container-fluid{padding-left:10px!important;padding-right:10px!important}
  }

  /* ── Mobile breakpoint (<576px) ── */
  @media (max-width: 575px) {
    body{font-size:.8rem}
    .topbar{padding:8px 12px;gap:8px}
    .topbar .brand{font-size:.95rem}
    .topbar .updated{display:none}
    .hero{padding:12px 14px;border-radius:8px}
    .hero-tbl{font-size:.74rem}
    .hero-tbl th,.hero-tbl td{padding:3px 5px}
    .card-hdr,.card-hdr-green,.card-hdr-red{font-size:.8rem;padding:8px 12px}
    .card-tab{padding:7px 12px;font-size:.78rem}
    .container-fluid{padding-left:8px!important;padding-right:8px!important}
    table thead th{padding:6px 7px;font-size:.72rem}
    table td{padding:5px 7px;font-size:.78rem}
    .filter-bar,.wl-add-bar,.stock-sel-bar{padding:6px 10px;gap:5px}
    .scanner-summary-bar{padding:6px 10px;gap:6px}
    .wl-sym-input{width:140px}
    .tbl-wrap{max-height:360px}
    .row.g-3{--bs-gutter-x:0.75rem;--bs-gutter-y:0.75rem}
  }
</style>
</head>
<body>

<!-- Top bar -->
<div class="topbar">
  <span class="brand">📊 Portfolio Tracker</span>
  <span class="updated">Last updated: <b>{{ last_updated }}</b></span>
  <form action="/fetch-now" method="post" class="mb-0">
    <button class="btn btn-sm btn-warning fw-semibold px-3">⟳ Fetch Now</button>
  </form>
  <button class="btn btn-sm btn-green fw-semibold px-3"
          data-bs-toggle="modal" data-bs-target="#addModal">＋ Add Stock</button>
</div>

{% set z_count = rows|selectattr('broker','equalto','Zerodha')|list|length %}
{% set g_count = rows|selectattr('broker','equalto','GoodWill')|list|length %}
<div class="container-fluid px-3 py-3">

  <!-- Hero — Portfolio Summary -->
  <div class="hero mb-3">
    <!-- Broker filter (moved here) -->
    <div class="hero-filter">
      <span class="flabel">🔍 Broker:</span>
      <button class="btn-hfilter active" id="f-all"      onclick="applyFilter('All')">All ({{ rows|length }})</button>
      <button class="btn-hfilter"        id="f-zerodha"  onclick="applyFilter('Zerodha')">Zerodha ({{ z_count }})</button>
      <button class="btn-hfilter gw"     id="f-goodwill" onclick="applyFilter('GoodWill')">GoodWill ({{ g_count }})</button>
      <span style="margin-left:auto;font-size:.78rem;opacity:.65" id="stock-count">{{ rows|length }} stocks</span>
    </div>
    <!-- Broker-wise summary table -->
    <div style="overflow-x:auto">
    <table class="hero-tbl">
      <thead>
        <tr>
          <th>Broker</th><th>Invested</th><th>Curr Value</th>
          <th>Total P&amp;L</th><th>Day P&amp;L</th>
        </tr>
      </thead>
      <tbody id="hero-summary-tbody">
        <!-- rendered by JS -->
      </tbody>
    </table>
    </div>
  </div>

  <!-- Gainers / Losers -->
  <div class="row g-3 mb-3">
    <div class="col-12 col-md-6">
      <div class="card">
        <div class="card-hdr-green">📈 Top 3 Gainers — Today's Move</div>
        <div class="p-0 table-responsive">
          <table class="table table-sm mb-0 mover-card green-tbl">
            <thead><tr>
              <th>Stock</th><th class="r">Prev ₹</th><th class="r">Today ₹</th><th class="r">Chg ₹</th><th class="r">Chg %</th>
            </tr></thead>
            <tbody id="gainers-tbody">
              {% for r in gainers %}
              <tr class="pos-bg">
                <td class="fw-bold">{{ r.stock }}</td>
                <td class="r">{{ '₹{:,.2f}'.format(r.prev_close) if r.prev_close else '—' }}</td>
                <td class="r">{{ '₹{:,.2f}'.format(r.close) if r.close else 'N/A' }}</td>
                <td class="r">{{ ('+₹' if r.day_chg_rs and r.day_chg_rs >= 0 else '₹') + '{:,.2f}'.format(r.day_chg_rs) if r.day_chg_rs is not none else '—' }}</td>
                <td class="r pos">{{ '+{:.2f}%'.format(r.day_pct) if r.day_pct >= 0 else '{:.2f}%'.format(r.day_pct) }}</td>
              </tr>
              {% else %}
              <tr><td colspan="5" class="text-center text-muted py-3">No data yet — click Fetch Now</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </div>
    </div>
    <div class="col-12 col-md-6">
      <div class="card">
        <div class="card-hdr-red">📉 Top 3 Losers — Today's Move</div>
        <div class="p-0 table-responsive">
          <table class="table table-sm mb-0 mover-card red-tbl">
            <thead><tr>
              <th>Stock</th><th class="r">Prev ₹</th><th class="r">Today ₹</th><th class="r">Chg ₹</th><th class="r">Chg %</th>
            </tr></thead>
            <tbody id="losers-tbody">
              {% for r in losers %}
              <tr class="neg-bg">
                <td class="fw-bold">{{ r.stock }}</td>
                <td class="r">{{ '₹{:,.2f}'.format(r.prev_close) if r.prev_close else '—' }}</td>
                <td class="r">{{ '₹{:,.2f}'.format(r.close) if r.close else 'N/A' }}</td>
                <td class="r">{{ '₹{:,.2f}'.format(r.day_chg_rs) if r.day_chg_rs is not none else '—' }}</td>
                <td class="r neg">{{ '{:.2f}%'.format(r.day_pct) }}</td>
              </tr>
              {% else %}
              <tr><td colspan="5" class="text-center text-muted py-3">No data yet — click Fetch Now</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <!-- All-Time Top 3 Gainers / Losers -->
  <div class="row g-3 mb-3">
    <div class="col-12 col-md-6">
      <div class="card">
        <div class="card-hdr-green">🏆 All-Time Top 3 Gainers (vs Avg Buy)</div>
        <div class="p-0 table-responsive">
          <table class="table table-sm mb-0 mover-card green-tbl">
            <thead><tr>
              <th>Stock</th><th class="r">Avg ₹</th><th class="r">Close ₹</th><th class="r">P&amp;L ₹</th><th class="r">P&amp;L %</th>
            </tr></thead>
            <tbody id="alltime-gainers-tbody">
              <tr><td colspan="5" class="text-center text-muted py-3">Loading…</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
    <div class="col-12 col-md-6">
      <div class="card">
        <div class="card-hdr-red">💔 All-Time Top 3 Losers (vs Avg Buy)</div>
        <div class="p-0 table-responsive">
          <table class="table table-sm mb-0 mover-card red-tbl">
            <thead><tr>
              <th>Stock</th><th class="r">Avg ₹</th><th class="r">Close ₹</th><th class="r">P&amp;L ₹</th><th class="r">P&amp;L %</th>
            </tr></thead>
            <tbody id="alltime-losers-tbody">
              <tr><td colspan="5" class="text-center text-muted py-3">Loading…</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <!-- Signal Scanner -->
  <div class="card mb-3" id="scanner-card">
    <div class="card-hdr d-flex align-items-center justify-content-between">
      <span>🎯 Signal Scanner — 20EMA / 50DMA / 200DMA Strategy</span>
      <span id="scanner-ts" style="font-size:.72rem;opacity:.7"></span>
    </div>
    <div class="scanner-summary-bar" id="scanner-pills">
      <span style="font-size:.78rem;color:#555">Loading signals…</span>
    </div>
    <div class="tbl-wrap" style="max-height:320px;overflow-x:auto">
      <table class="table table-hover table-sm mb-0" id="scanner-table">
        <thead>
          <tr>
            <th class="sortable" onclick="sortScannerTable('stock')">Stock <span class="si"></span></th>
            <th class="r sortable" onclick="sortScannerTable('cmp')">CMP ₹ <span class="si"></span></th>
            <th class="r sortable d-none d-md-table-cell" onclick="sortScannerTable('ema20')">20 EMA <span class="si"></span></th>
            <th class="r sortable d-none d-md-table-cell" onclick="sortScannerTable('ema_dist_pct')">EMA Dist % <span class="si"></span></th>
            <th class="r sortable" onclick="sortScannerTable('rsi14')">RSI 14 <span class="si"></span></th>
            <th class="r sortable" onclick="sortScannerTable('vol_ratio')">Vol× <span class="si"></span></th>
            <th class="r sortable d-none d-md-table-cell" onclick="sortScannerTable('dma50')">50 DMA <span class="si"></span></th>
            <th class="r sortable d-none d-md-table-cell" onclick="sortScannerTable('dma200')">200 DMA <span class="si"></span></th>
            <th class="sortable d-none d-md-table-cell" onclick="sortScannerTable('dma200_slope')">200 Slope <span class="si"></span></th>
            <th class="r sortable d-none d-md-table-cell" onclick="sortScannerTable('stop')">Stop ₹ <span class="si"></span></th>
            <th class="r sortable d-none d-md-table-cell" onclick="sortScannerTable('size')"
                title="Suggested qty risking 1% of portfolio value: (1% × portfolio) ÷ (CMP − stop)">Size @1% <span class="si"></span></th>
            <th class="sortable" onclick="sortScannerTable('signal')">Signal <span class="si"></span></th>
          </tr>
        </thead>
        <tbody id="scanner-tbody">
          <tr><td colspan="12" class="text-center text-muted py-3">⏳ Fetching data…</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Exit Radar — holdings needing exit review -->
  <div class="card mb-3" id="exit-radar-card">
    <div class="card-hdr-red d-flex align-items-center justify-content-between">
      <span>🚪 Exit Radar — Holdings Needing Review</span>
      <span style="font-size:.72rem;opacity:.8">stop = 50 DMA · profit zone = RSI&gt;70 &amp; +8% over 20EMA</span>
    </div>
    <div class="scanner-summary-bar" id="exit-pills">
      <span style="font-size:.78rem;color:#555">Loading…</span>
    </div>
    <div class="tbl-wrap" style="max-height:320px">
      <table class="table table-hover table-sm mb-0" id="exit-table">
        <thead>
          <tr>
            <th class="sortable" onclick="sortExitTable('stock')">Stock <span class="si"></span></th>
            <th class="r sortable" onclick="sortExitTable('cmp')">CMP ₹ <span class="si"></span></th>
            <th class="r sortable" onclick="sortExitTable('pnl_pct')">P&amp;L % <span class="si"></span></th>
            <th class="r sortable d-none d-md-table-cell" onclick="sortExitTable('rsi14')">RSI 14 <span class="si"></span></th>
            <th class="r sortable d-none d-md-table-cell" onclick="sortExitTable('vol_ratio')">Vol× <span class="si"></span></th>
            <th class="r sortable d-none d-md-table-cell" onclick="sortExitTable('stop')">Stop ₹ <span class="si"></span></th>
            <th class="r sortable d-none d-md-table-cell" onclick="sortExitTable('off_high_pct')">Off 52w High <span class="si"></span></th>
            <th class="r sortable d-none d-md-table-cell" onclick="sortExitTable('ltcg')"
                title="Indian equity tax: gains turn long-term (12.5%) after 1 year; short-term is 20%">LTCG <span class="si"></span></th>
            <th class="sortable d-none d-md-table-cell" onclick="sortExitTable('signal')">Signal <span class="si"></span></th>
            <th class="sortable" onclick="sortExitTable('score')">Why review <span class="si"></span></th>
          </tr>
        </thead>
        <tbody id="exit-tbody">
          <tr><td colspan="10" class="text-center text-muted py-3">⏳ Fetching data…</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Line Chart -->
  <div class="card mb-3">
    <div class="card-hdr">📈 All Stocks — P&amp;L % by Day (vs Avg Buy Price)</div>
    <!-- Stock selector bar -->
    <div class="stock-sel-bar">
      <span class="slabel">📊 Stocks:</span>
      <button class="btn-sel dis-all" onclick="disableAllStocks()">Disable All</button>
      <button class="btn-sel en-all"  onclick="enableAllStocks()">Enable All</button>
      <span class="cb-vis" id="cb-vis-count"></span>
      <span id="cb-toggle" onclick="toggleCbPanel()">▸ Pick individual stocks</span>
    </div>
    <!-- Collapsible individual checkboxes -->
    <div class="cb-panel" id="cb-panel">
      <div class="cb-grid" id="cb-grid"></div>
    </div>
    <div class="card-body">
      <div class="chart-wrap">
        <canvas id="pnlChart"></canvas>
      </div>
    </div>
  </div>

  <!-- Holdings + Technical Tabs -->
  <div class="card mb-3">
    <div class="card-hdr d-flex align-items-center justify-content-between" style="padding:0 16px 0 0">
      <div class="card-tabs">
        <button class="card-tab active" id="tab-holdings"  onclick="switchHoldingsTab('holdings')">📋 Holdings</button>
        <button class="card-tab"        id="tab-technical" onclick="switchHoldingsTab('technical')">📊 Technical</button>
        <button class="card-tab"        id="tab-watchlist" onclick="switchHoldingsTab('watchlist')">👁 Watchlist</button>
      </div>
      <span class="fw-normal opacity-75" style="font-size:.78rem" id="filter-label">All {{ rows|length }} stocks</span>
    </div>

    <!-- ── HOLDINGS PANEL ── -->
    <div id="holdings-panel">
    <div class="tbl-wrap">
      <table class="table table-hover table-sm mb-0">
        <thead>
          <tr>
            <th class="sortable" data-col="stock"    onclick="sortTable('stock')"   >Stock    <span class="si"></span></th>
            <th class="sortable d-none d-md-table-cell" data-col="exchange" onclick="sortTable('exchange')" >Exch     <span class="si"></span></th>
            <th class="sortable d-none d-md-table-cell" data-col="broker"   onclick="sortTable('broker')"  >Broker   <span class="si"></span></th>
            <th class="sortable r d-none d-md-table-cell" data-col="qty"    onclick="sortTable('qty')"     >Qty      <span class="si"></span></th>
            <th class="sortable r d-none d-md-table-cell" data-col="avg"    onclick="sortTable('avg')"     >Avg ₹    <span class="si"></span></th>
            <th class="sortable r" data-col="close"  onclick="sortTable('close')"   >Close ₹  <span class="si"></span></th>
            <th class="sortable r d-none d-md-table-cell" data-col="pnl_rs" onclick="sortTable('pnl_rs')"  >P&amp;L ₹ <span class="si"></span></th>
            <th class="sortable r" data-col="pnl_pct"onclick="sortTable('pnl_pct')" >P&amp;L % <span class="si"></span></th>
            <th class="sortable r" data-col="day_chg"onclick="sortTable('day_chg')" >Day %    <span class="si"></span></th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody id="holdings-tbody">
          {% for r in rows %}
          {% if r.pnl_pct is not none %}
            {% if r.pnl_pct > 0.5 %}{% set rc = 'pos-bg' %}
            {% elif r.pnl_pct < -0.5 %}{% set rc = 'neg-bg' %}
            {% else %}{% set rc = 'neu-bg' %}{% endif %}
          {% else %}{% set rc = '' %}{% endif %}
          <tr class="{{ rc }}" data-broker="{{ r.broker }}" data-stock="{{ r.stock }}">
            <td class="fw-bold">{{ r.stock }}</td>
            <td class="d-none d-md-table-cell"><span class="badge-{{ r.exchange|lower }}">{{ r.exchange }}</span></td>
            <td class="d-none d-md-table-cell">{{ r.broker }}</td>
            <td class="r d-none d-md-table-cell">{{ r.qty|int|string }}</td>
            <td class="r d-none d-md-table-cell">₹{{ '{:,.2f}'.format(r.avg) }}</td>
            <td class="r">{{ '₹{:,.2f}'.format(r.close) if r.close else 'N/A' }}</td>
            <td class="r d-none d-md-table-cell {{ 'pos' if r.pnl_rs and r.pnl_rs > 0 else ('neg' if r.pnl_rs and r.pnl_rs < 0 else '') }}">
              {% if r.pnl_rs is not none %}{{ '+' if r.pnl_rs >= 0 else '' }}₹{{ '{:,.0f}'.format(r.pnl_rs) }}{% else %}N/A{% endif %}
            </td>
            <td class="r {{ 'pos' if r.pnl_pct and r.pnl_pct > 0 else ('neg' if r.pnl_pct and r.pnl_pct < 0 else '') }}">
              {% if r.pnl_pct is not none %}{{ '+' if r.pnl_pct >= 0 else '' }}{{ '{:.2f}'.format(r.pnl_pct) }}%{% else %}N/A{% endif %}
            </td>
            <td class="r {{ 'pos' if r.day_chg and r.day_chg > 0 else ('neg' if r.day_chg and r.day_chg < 0 else '') }}">
              {% if r.day_chg is not none %}{{ '+' if r.day_chg >= 0 else '' }}{{ '{:.2f}'.format(r.day_chg) }}%{% else %}—{% endif %}
            </td>
            <td style="white-space:nowrap">
              <!-- Edit icon button -->
              <button type="button" class="icon-btn" title="Edit {{ r.stock }}"
                onclick="openEdit('{{ r.stock }}','{{ r.qty }}','{{ r.avg }}','{{ r.yahoo }}','{{ r.broker }}','{{ r.buy_date or '' }}')">
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                  <!-- Circular arrows -->
                  <path d="M12 3C7.03 3 3 7.03 3 12s4.03 9 9 9c2.39 0 4.56-.93 6.18-2.44"
                        stroke="#1F3864" stroke-width="2.2" stroke-linecap="round"/>
                  <polyline points="21,3 18.5,7.5 14,5"
                        stroke="#1F3864" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
                  <!-- Pencil body -->
                  <path d="M14.06 9.02l.92.92-8.06 8.06H6v-.92l8.06-8.06z" fill="#5bb8f5"/>
                  <!-- Pencil tip cap -->
                  <path d="M17.66 6c-.44 0-.86.18-1.18.48l-1.42 1.42 3 3 1.42-1.42c.66-.64.66-1.7 0-2.36l-.64-.64c-.32-.3-.74-.48-1.18-.48z" fill="#1a6db5"/>
                </svg>
              </button>
              <!-- Delete icon button -->
              <form action="/delete-stock" method="post" class="d-inline">
                <input type="hidden" name="symbol" value="{{ r.stock }}">
                <button type="button" class="icon-btn" title="Delete {{ r.stock }}"
                        onclick="confirmDelete(this, '{{ r.stock }}')">
                  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <!-- Lid top bar -->
                    <line x1="3" y1="6" x2="21" y2="6" stroke="#5bb8f5" stroke-width="2.2" stroke-linecap="round"/>
                    <!-- Handle -->
                    <path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"
                          stroke="#5bb8f5" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                    <!-- Body filled -->
                    <path d="M19 6l-1.4 14.1a1 1 0 0 1-1 .9H7.4a1 1 0 0 1-1-.9L5 6z" fill="#1F3864"/>
                    <!-- Inner lines -->
                    <line x1="10" y1="11" x2="10" y2="17" stroke="#5bb8f5" stroke-width="1.5" stroke-linecap="round"/>
                    <line x1="14" y1="11" x2="14" y2="17" stroke="#5bb8f5" stroke-width="1.5" stroke-linecap="round"/>
                  </svg>
                </button>
              </form>
            </td>
          </tr>
          {% endfor %}
          <tr class="total-row">
            <td colspan="6">PORTFOLIO TOTAL</td>
            <td class="r" colspan="2" id="portfolio-total">
              {{ '+' if total_pnl >= 0 else '' }}₹{{ '{:,.0f}'.format(total_pnl) }}
            </td>
            <td colspan="2"></td>
          </tr>
        </tbody>
      </table>
    </div>
    </div><!-- /holdings-panel -->

    <!-- ── TECHNICAL PANEL ── -->
    <div id="technical-panel" style="display:none">
      <div style="display:flex;align-items:center;justify-content:flex-end;
                  padding:8px 16px;background:#eef0f4;border-bottom:1px solid #dde2ea;gap:8px">
        <span style="font-size:.75rem;color:#666" id="dma-cache-time"></span>
        <button class="dma-refresh-btn" onclick="loadDmaData(true)">⟳ Refresh DMA</button>
      </div>
      <div class="tbl-wrap">
        <table class="table table-hover table-sm mb-0" id="dma-table">
          <thead>
            <tr>
              <th class="sortable" onclick="sortDmaTable('stock')">Stock <span class="si"></span></th>
              <th class="r sortable" onclick="sortDmaTable('cmp')">CMP ₹ <span class="si"></span></th>
              <th class="r sortable d-none d-md-table-cell" onclick="sortDmaTable('ema20')">20 EMA <span class="si"></span></th>
              <th class="r sortable d-none d-md-table-cell" onclick="sortDmaTable('ema_dist_pct')">EMA Dist % <span class="si"></span></th>
              <th class="r sortable" onclick="sortDmaTable('rsi14')">RSI 14 <span class="si"></span></th>
              <th class="r sortable" onclick="sortDmaTable('vol_ratio')">Vol× <span class="si"></span></th>
              <th class="r sortable d-none d-md-table-cell" onclick="sortDmaTable('dma50')">50 DMA <span class="si"></span></th>
              <th class="r sortable d-none d-md-table-cell" onclick="sortDmaTable('dma200')">200 DMA <span class="si"></span></th>
              <th class="sortable d-none d-md-table-cell" onclick="sortDmaTable('dma200_slope')">200 Slope <span class="si"></span></th>
              <th class="sortable d-none d-md-table-cell" onclick="sortDmaTable('cross')">Cross <span class="si"></span></th>
              <th class="sortable" onclick="sortDmaTable('signal')">Signal <span class="si"></span></th>
              <th class="r sortable d-none d-md-table-cell" onclick="sortDmaTable('stop')">Stop ₹ <span class="si"></span></th>
            </tr>
          </thead>
          <tbody id="dma-tbody">
            <tr><td colspan="7" class="dma-loading">Click the tab to load DMA data…</td></tr>
          </tbody>
        </table>
      </div>
    </div><!-- /technical-panel -->

    <!-- ── WATCHLIST PANEL ── -->
    <div id="watchlist-panel" style="display:none">
      <!-- Add stock bar -->
      <div class="wl-add-bar">
        <input type="text" id="wl-input" class="wl-sym-input"
               placeholder="Yahoo symbol e.g. NLCINDIA.NS"
               onkeydown="if(event.key==='Enter') wlAdd()">
        <button class="wl-add-btn" onclick="wlAdd()">+ Add</button>
        <button class="dma-refresh-btn" onclick="loadWatchlistDma(true)" style="margin-left:4px">⟳ Refresh</button>
        <span id="wl-msg" style="font-size:.75rem;color:#567044;margin-left:4px"></span>
      </div>
      <!-- Watchlist table -->
      <div class="tbl-wrap">
        <table class="table table-hover table-sm mb-0" id="wl-table">
          <thead>
            <tr>
              <th class="sortable" onclick="sortWlTable('symbol')">Symbol <span class="si"></span></th>
              <th class="r sortable" onclick="sortWlTable('cmp')">CMP ₹ <span class="si"></span></th>
              <th class="r sortable d-none d-md-table-cell" onclick="sortWlTable('ema20')">20 EMA <span class="si"></span></th>
              <th class="r sortable d-none d-md-table-cell" onclick="sortWlTable('ema_dist_pct')">EMA Dist % <span class="si"></span></th>
              <th class="r sortable" onclick="sortWlTable('rsi14')">RSI 14 <span class="si"></span></th>
              <th class="r sortable" onclick="sortWlTable('vol_ratio')">Vol× <span class="si"></span></th>
              <th class="r sortable d-none d-md-table-cell" onclick="sortWlTable('dma50')">50 DMA <span class="si"></span></th>
              <th class="r sortable d-none d-md-table-cell" onclick="sortWlTable('dma200')">200 DMA <span class="si"></span></th>
              <th class="sortable d-none d-md-table-cell" onclick="sortWlTable('dma200_slope')">200 Slope <span class="si"></span></th>
              <th class="sortable d-none d-md-table-cell" onclick="sortWlTable('cross')">Cross <span class="si"></span></th>
              <th class="sortable" onclick="sortWlTable('signal')">Signal <span class="si"></span></th>
              <th class="r sortable d-none d-md-table-cell" onclick="sortWlTable('stop')">Stop ₹ <span class="si"></span></th>
              <th></th>
            </tr>
          </thead>
          <tbody id="wl-tbody">
            {% if watchlist %}
            <tr><td colspan="13" class="text-center text-muted py-3">⏳ Click tab to load signal data…</td></tr>
            {% else %}
            <tr><td colspan="13" class="text-center text-muted py-3">No watchlist stocks yet — add a Yahoo symbol above.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>
    </div><!-- /watchlist-panel -->

  </div><!-- /card -->

</div><!-- /container -->

<!-- ── Add Stock Modal ─────────────────────────────────────────────────── -->
<div class="modal fade" id="addModal" tabindex="-1">
  <div class="modal-dialog">
    <div class="modal-content">
      <div class="modal-header btn-navy">
        <h5 class="modal-title text-white">＋ Add New Stock</h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <form action="/add-stock" method="post">
        <div class="modal-body">
          <div class="mb-3">
            <label class="form-label fw-semibold">Stock Symbol <span class="text-danger">*</span></label>
            <input type="text" class="form-control" name="symbol"
                   placeholder="e.g. RELIANCE" required style="text-transform:uppercase">
          </div>
          <div class="mb-3">
            <label class="form-label fw-semibold">Yahoo Finance Symbol</label>
            <input type="text" class="form-control" name="yahoo"
                   placeholder="e.g. RELIANCE.NS  or  RELIANCE.BO">
            <div class="form-text">NSE stocks → <code>.NS</code> &nbsp;|&nbsp; BSE stocks → <code>.BO</code>
              &nbsp;|&nbsp; Leave blank if unavailable.</div>
          </div>
          <div class="row g-2">
            <div class="col">
              <label class="form-label fw-semibold">Avg Buy Price ₹ <span class="text-danger">*</span></label>
              <input type="number" step="0.000001" min="0" class="form-control"
                     name="avg" placeholder="0.00" required>
            </div>
            <div class="col">
              <label class="form-label fw-semibold">Quantity <span class="text-danger">*</span></label>
              <input type="number" min="1" class="form-control" name="qty" placeholder="0" required>
            </div>
          </div>
          <div class="mb-3 mt-2">
            <label class="form-label fw-semibold">Buy Date</label>
            <input type="date" class="form-control" name="buy_date">
            <div class="form-text">Optional — enables the LTCG (1-year) tax timer on the Exit Radar.</div>
          </div>
          <div class="mb-3 mt-2">
            <label class="form-label fw-semibold">Broker <span class="text-danger">*</span></label>
            <select class="form-select" name="broker" required>
              <option value="Zerodha">Zerodha</option>
              <option value="GoodWill">GoodWill</option>
            </select>
          </div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
          <button type="submit" class="btn btn-navy">Save Stock</button>
        </div>
      </form>
    </div>
  </div>
</div>

<!-- ── Edit Stock Modal ────────────────────────────────────────────────── -->
<div class="modal fade" id="editModal" tabindex="-1">
  <div class="modal-dialog">
    <div class="modal-content">
      <div class="modal-header btn-green">
        <h5 class="modal-title text-white">✏️ Edit Stock</h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <form action="/update-stock" method="post">
        <input type="hidden" name="symbol" id="editSymbol">
        <div class="modal-body">
          <div class="mb-3">
            <label class="form-label fw-semibold">Stock Symbol</label>
            <input type="text" class="form-control bg-light" id="editSymbolDisplay" disabled>
          </div>
          <div class="mb-3">
            <label class="form-label fw-semibold">Yahoo Finance Symbol</label>
            <input type="text" class="form-control" name="yahoo" id="editYahoo">
            <div class="form-text">NSE → <code>.NS</code> &nbsp;|&nbsp; BSE → <code>.BO</code></div>
          </div>
          <div class="row g-2">
            <div class="col">
              <label class="form-label fw-semibold">Avg Buy Price ₹</label>
              <input type="number" step="0.000001" min="0" class="form-control"
                     name="avg" id="editAvg" required>
            </div>
            <div class="col">
              <label class="form-label fw-semibold">Quantity</label>
              <input type="number" min="0" class="form-control" name="qty" id="editQty" required>
            </div>
          </div>
          <div class="mb-3 mt-2">
            <label class="form-label fw-semibold">Buy Date</label>
            <input type="date" class="form-control" name="buy_date" id="editBuyDate">
            <div class="form-text">Optional — enables the LTCG (1-year) tax timer on the Exit Radar.</div>
          </div>
          <div class="mb-3 mt-2">
            <label class="form-label fw-semibold">Broker</label>
            <select class="form-select" name="broker" id="editBroker">
              <option value="Zerodha">Zerodha</option>
              <option value="GoodWill">GoodWill</option>
            </select>
          </div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
          <button type="submit" class="btn btn-green text-white">Update</button>
        </div>
      </form>

      <!-- ── Add purchase (averages into the saved holding) ── -->
      <form action="/add-purchase" method="post" class="border-top" style="background:#f8f9fc">
        <input type="hidden" name="symbol" id="apSymbol">
        <div class="px-3 pt-3 pb-1">
          <div class="fw-semibold mb-2" style="font-size:.88rem">➕ Add purchase (average in)</div>
          <div class="row g-2">
            <div class="col">
              <label class="form-label" style="font-size:.78rem">Extra Qty</label>
              <input type="number" min="1" class="form-control form-control-sm"
                     name="add_qty" id="apQty" placeholder="0" oninput="previewAvg()">
            </div>
            <div class="col">
              <label class="form-label" style="font-size:.78rem">Buy Price ₹</label>
              <input type="number" step="0.000001" min="0" class="form-control form-control-sm"
                     name="add_price" id="apPrice" placeholder="0.00" oninput="previewAvg()">
            </div>
          </div>
          <div class="form-text mt-2" id="apPreview">
            Adds to the saved qty and recalculates the weighted average price.
          </div>
        </div>
        <div class="px-3 pb-3 text-end">
          <button type="submit" class="btn btn-sm btn-navy">Add &amp; Average</button>
        </div>
      </form>
    </div>
  </div>
</div>

<!-- Toast notification -->
<div class="toast-bar" id="toastBar">⟳ Fetching prices… refresh in ~10 seconds</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
<script>
// ── Raw data from server ───────────────────────────────────────────────────
const rawData  = {{ chart_data  | tojson }};
const rawClose = {{ chart_close | tojson }};
const dates    = {{ dates       | tojson }};
const stocks  = {{ stocks     | tojson }};
const allRows = {{ rows       | tojson }};

const PALETTE = [
  '#1f77b4','#ff7f0e','#2ca02c','#d62728','#9467bd','#8c564b','#e377c2',
  '#7f7f7f','#bcbd22','#17becf','#aec7e8','#ffbb78','#98df8a','#ff9896',
  '#c5b0d5','#c49c94','#f7b6d2','#c7c7c7','#dbdb8d','#9edae5','#393b79',
  '#637939','#8c6d31','#843c39','#7b4173','#5254a3','#6b6ecf','#b5cf6b'
];

// ── State ──────────────────────────────────────────────────────────────────
let pnlChart        = null;
let activeStocks    = [...stocks];     // stocks in current broker filter
let stockVisible    = {};              // {stock: true/false} — checkbox state
stocks.forEach(s => stockVisible[s] = true);

let sortState = { col: null, dir: 1 };
let activeBroker = 'All';

// ── Chart options ──────────────────────────────────────────────────────────
const CHART_OPTIONS = {
  responsive: true,
  maintainAspectRatio: false,
  interaction: { mode: 'index', intersect: false },
  plugins: {
    legend: { display: false },   // ← legends removed
    tooltip: {
      callbacks: {
        label: ctx => {
          const pct   = ctx.parsed.y != null ? ctx.parsed.y.toFixed(2) + '%' : 'N/A';
          const date  = ctx.label;
          const close = rawClose[date] ? rawClose[date][ctx.dataset.label] : null;
          const px    = close != null ? '  ₹' + close.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2}) : '';
          return `${ctx.dataset.label}: ${pct}${px}`;
        }
      }
    }
  },
  scales: {
    x: { title: { display: false }, ticks: { font: { size: 10 } } },  // ← "Date" label removed
    y: {
      title: { display: true, text: 'P&L %' },
      ticks: { callback: v => v.toFixed(1) + '%', font: { size: 10 } },
      grid: { color: 'rgba(0,0,0,0.06)' }
    }
  }
};

// ── Chart rendering ────────────────────────────────────────────────────────
function buildDatasets(filteredStocks) {
  return filteredStocks.map(stock => {
    const i = stocks.indexOf(stock);
    return {
      label: stock,
      data: dates.map(d => (rawData[d] && rawData[d][stock] != null) ? rawData[d][stock] : null),
      borderColor: PALETTE[i % PALETTE.length],
      backgroundColor: 'transparent',
      borderWidth: 1.5,
      pointRadius: dates.length <= 10 ? 3 : 1,
      tension: 0.2,
      spanGaps: true,
    };
  });
}

// ── NIFTY 50 benchmark overlay ──────────────────────────────────────────────
let benchData = null;   // {dateLabel: nifty close}
fetch('/api/benchmark')
  .then(r => r.json())
  .then(d => {
    if (d && !d.error && Object.keys(d).length) {
      benchData = d;
      renderChart(activeStocks);   // re-render with the overlay
    }
  })
  .catch(() => {});

function benchDataset() {
  if (!benchData) return null;
  let base = null;   // first chart date with a Nifty close = 0% baseline
  const pts = dates.map(d => {
    const c = benchData[d];
    if (c == null) return null;
    if (base == null) base = c;
    return +((c / base - 1) * 100).toFixed(2);
  });
  if (base == null) return null;
  return {
    label: 'NIFTY 50', data: pts,
    borderColor: '#555', borderDash: [6, 4], borderWidth: 2,
    backgroundColor: 'transparent', pointRadius: 0, tension: 0.2, spanGaps: true,
  };
}

function renderChart(filteredStocks) {
  activeStocks = filteredStocks;
  if (pnlChart) pnlChart.destroy();
  const dsets = buildDatasets(filteredStocks);
  const bench = benchDataset();
  if (bench) dsets.push(bench);   // appended last so stock indexes stay stable
  pnlChart = new Chart(document.getElementById('pnlChart'), {
    type: 'line',
    data: { labels: dates, datasets: dsets },
    options: CHART_OPTIONS,
  });
  // Apply current visibility state
  filteredStocks.forEach((stock, i) => {
    if (!stockVisible[stock]) {
      pnlChart.getDatasetMeta(i).hidden = true;
    }
  });
  pnlChart.update('none');
  buildCheckboxPanel(filteredStocks);
}

// ── Chart stock selector (checkbox panel) ──────────────────────────────────
function buildCheckboxPanel(filteredStocks) {
  const visCount = filteredStocks.filter(s => stockVisible[s]).length;
  document.getElementById('cb-vis-count').textContent =
    `${visCount} / ${filteredStocks.length} visible`;

  const grid = document.getElementById('cb-grid');
  grid.innerHTML = filteredStocks.map(stock => {
    const i     = stocks.indexOf(stock);
    const color = PALETTE[i % PALETTE.length];
    const chk   = stockVisible[stock] ? 'checked' : '';
    return `<label class="cb-item">
      <input type="checkbox" value="${stock}" ${chk}
        style="accent-color:${color}"
        onchange="toggleStock('${stock}', this.checked)">
      <span style="color:${color};font-weight:600;font-size:.72rem">${stock}</span>
    </label>`;
  }).join('');
}

function toggleCbPanel() {
  const panel  = document.getElementById('cb-panel');
  const toggle = document.getElementById('cb-toggle');
  const open   = panel.classList.toggle('open');
  toggle.textContent = open ? '▾ Hide stock list' : '▸ Pick individual stocks';
}

function disableAllStocks() {
  activeStocks.forEach(s => stockVisible[s] = false);
  activeStocks.forEach((s, i) => {
    const idx = pnlChart.data.datasets.findIndex(d => d.label === s);
    if (idx >= 0) pnlChart.getDatasetMeta(idx).hidden = true;
  });
  pnlChart.update();
  buildCheckboxPanel(activeStocks);
  // Auto-open panel so user can pick stocks
  const panel = document.getElementById('cb-panel');
  if (!panel.classList.contains('open')) toggleCbPanel();
}

function enableAllStocks() {
  activeStocks.forEach(s => stockVisible[s] = true);
  activeStocks.forEach((s, i) => {
    const idx = pnlChart.data.datasets.findIndex(d => d.label === s);
    if (idx >= 0) pnlChart.getDatasetMeta(idx).hidden = false;
  });
  pnlChart.update();
  buildCheckboxPanel(activeStocks);
}

function toggleStock(stock, visible) {
  stockVisible[stock] = visible;
  const idx = pnlChart.data.datasets.findIndex(d => d.label === stock);
  if (idx >= 0) {
    pnlChart.getDatasetMeta(idx).hidden = !visible;
    pnlChart.update();
  }
  buildCheckboxPanel(activeStocks);
}

// ── Sort ───────────────────────────────────────────────────────────────────
const COL_GETTER = {
  stock:    r => r.stock,
  exchange: r => r.exchange,
  broker:   r => r.broker,
  qty:      r => r.qty      ?? null,
  avg:      r => r.avg      ?? null,
  close:    r => r.close    ?? null,
  pnl_rs:   r => r.pnl_rs  ?? null,
  pnl_pct:  r => r.pnl_pct ?? null,
  day_chg:  r => r.day_chg  ?? null,
};

function sortTable(col) {
  sortState.dir = (sortState.col === col) ? -sortState.dir : 1;
  sortState.col = col;

  // Update header indicators
  document.querySelectorAll('th.sortable').forEach(th => th.classList.remove('sort-asc','sort-desc'));
  const activeTh = document.querySelector(`th[data-col="${col}"]`);
  if (activeTh) activeTh.classList.add(sortState.dir === 1 ? 'sort-asc' : 'sort-desc');

  const getter   = COL_GETTER[col];
  const tbody    = document.getElementById('holdings-tbody');
  const totalRow = tbody.querySelector('.total-row');
  const dataRows = [...tbody.querySelectorAll('tr[data-stock]')];

  // Map stock → DOM row
  const rowMap = {};
  dataRows.forEach(tr => rowMap[tr.dataset.stock] = tr);

  // Sort allRows, nulls always sink to bottom
  const sorted = [...allRows].sort((a, b) => {
    const va = getter(a), vb = getter(b);
    if (va === null && vb === null) return 0;
    if (va === null) return 1;
    if (vb === null) return -1;
    if (typeof va === 'string') return sortState.dir * va.localeCompare(vb);
    return sortState.dir * (va - vb);
  });

  // Re-append in sorted order (hidden rows stay hidden via display:none)
  sorted.forEach(r => {
    if (rowMap[r.stock]) tbody.insertBefore(rowMap[r.stock], totalRow);
  });
}

// ── Hero summary (broker-wise Invested / Curr Value / P&L / Day P&L) ─────
function calcStats(rows) {
  const inv  = rows.reduce((s, r) => s + r.avg * r.qty, 0);
  const curr = rows.reduce((s, r) => s + (r.close != null ? r.close * r.qty : r.avg * r.qty), 0);
  const pnl  = rows.reduce((s, r) => s + (r.pnl_rs  || 0), 0);
  const day  = rows.reduce((s, r) => s + (r.day_chg_rs != null ? r.day_chg_rs * r.qty : 0), 0);
  return { inv, curr, pnl, day };
}

function fmtInv(v)  { return '₹' + Math.round(v).toLocaleString('en-IN'); }
function fmtPnlH(v) {
  const cls = v > 0 ? 'hpos' : v < 0 ? 'hneg' : 'hneu';
  const str = (v >= 0 ? '+' : '') + '₹' + Math.round(v).toLocaleString('en-IN');
  return `<span class="${cls}">${str}</span>`;
}

function renderHeroSummary(broker) {
  const brokers = broker === 'All' ? ['Zerodha', 'GoodWill'] : [broker];
  let html = '';
  brokers.forEach(b => {
    const rows = allRows.filter(r => r.broker === b);
    const s = calcStats(rows);
    const pnlPct = s.inv > 0 ? (s.pnl / s.inv * 100) : 0;
    html += `<tr>
      <td>${b}</td>
      <td>${fmtInv(s.inv)}</td>
      <td>${fmtInv(s.curr)}</td>
      <td>${fmtPnlH(s.pnl)} <small style="opacity:.65;font-size:.7rem">${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(1)}%</small></td>
      <td>${fmtPnlH(s.day)}</td>
    </tr>`;
  });
  // Total row
  const allFiltered = broker === 'All' ? allRows : allRows.filter(r => r.broker === broker);
  const t = calcStats(allFiltered);
  const tPct = t.inv > 0 ? (t.pnl / t.inv * 100) : 0;
  html += `<tr class="hero-total">
    <td>TOTAL</td>
    <td>${fmtInv(t.inv)}</td>
    <td>${fmtInv(t.curr)}</td>
    <td>${fmtPnlH(t.pnl)} <small style="opacity:.65;font-size:.7rem">${tPct >= 0 ? '+' : ''}${tPct.toFixed(1)}%</small></td>
    <td>${fmtPnlH(t.day)}</td>
  </tr>`;
  document.getElementById('hero-summary-tbody').innerHTML = html;
}

// ── All-Time Top 3 Gainers / Losers ───────────────────────────────────────
function renderAllTimeMovers(filteredRows) {
  const withData = filteredRows.filter(r => r.pnl_pct != null);
  const desc     = [...withData].sort((a, b) => b.pnl_pct - a.pnl_pct);
  const gainers  = desc.slice(0, 3);
  const losers   = desc.slice(-3).reverse();
  const noData   = '<tr><td colspan="5" class="text-center text-muted py-3">No data yet — click Fetch Now</td></tr>';

  const rowHtml = (r, cls) => `<tr class="${cls}">
    <td class="fw-bold">${r.stock}</td>
    <td class="r">${fmtRs2(r.avg)}</td>
    <td class="r">${r.close != null ? fmtRs2(r.close) : 'N/A'}</td>
    <td class="r ${r.pnl_rs > 0 ? 'pos' : 'neg'}">${r.pnl_rs != null ? (r.pnl_rs >= 0 ? '+' : '') + '₹' + Math.round(r.pnl_rs).toLocaleString('en-IN') : 'N/A'}</td>
    <td class="r ${r.pnl_pct > 0 ? 'pos' : 'neg'}">${r.pnl_pct != null ? (r.pnl_pct >= 0 ? '+' : '') + r.pnl_pct.toFixed(2) + '%' : 'N/A'}</td>
  </tr>`;

  document.getElementById('alltime-gainers-tbody').innerHTML =
    gainers.length ? gainers.map(r => rowHtml(r, 'pos-bg')).join('') : noData;
  document.getElementById('alltime-losers-tbody').innerHTML =
    losers.length  ? losers.map(r => rowHtml(r, 'neg-bg')).join('') : noData;
}

// ── Gainers / Losers recalc ────────────────────────────────────────────────
function fmtRs2(v)   { return v == null ? '—' : '₹' + v.toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2}); }
function fmtChgRs(v) { return v == null ? '—' : (v >= 0 ? '+₹' : '−₹') + Math.abs(v).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2}); }
function fmtPct(v)   { return v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(2) + '%'; }

function renderMovers(filteredRows) {
  let scored = filteredRows.filter(r => r.day_chg != null).map(r => ({ ...r, _s: r.day_chg }));
  if (!scored.length)
    scored = filteredRows.filter(r => r.pnl_pct != null).map(r => ({ ...r, _s: r.pnl_pct }));

  const desc    = [...scored].sort((a, b) => b._s - a._s);
  const gainers = desc.slice(0, 3);
  const losers  = desc.slice(-3).reverse();
  const noData  = '<tr><td colspan="5" class="text-center text-muted py-3">No data yet — click Fetch Now</td></tr>';

  const rowHtml = (r, cls) => {
    const pct = r._s;
    return `<tr class="${cls}">
      <td class="fw-bold">${r.stock}</td>
      <td class="r">${fmtRs2(r.prev_close)}</td>
      <td class="r">${fmtRs2(r.close)}</td>
      <td class="r">${fmtChgRs(r.day_chg_rs)}</td>
      <td class="r ${pct >= 0 ? 'pos' : 'neg'}">${fmtPct(pct)}</td>
    </tr>`;
  };

  document.getElementById('gainers-tbody').innerHTML =
    gainers.length ? gainers.map(r => rowHtml(r, 'pos-bg')).join('') : noData;
  document.getElementById('losers-tbody').innerHTML =
    losers.length  ? losers.map(r => rowHtml(r, 'neg-bg')).join('') : noData;
}

// ── Portfolio total recalc ─────────────────────────────────────────────────
function renderTotal(filteredRows) {
  const total = filteredRows.reduce((s, r) => s + (r.pnl_rs || 0), 0);
  const el    = document.getElementById('portfolio-total');
  el.textContent = (total >= 0 ? '+' : '') + '₹' + Math.round(total).toLocaleString('en-IN');
}

// ── Broker filter ──────────────────────────────────────────────────────────
function applyFilter(broker) {
  activeBroker = broker;
  document.querySelectorAll('.btn-hfilter').forEach(b => b.classList.remove('active'));
  document.getElementById('f-' + broker.toLowerCase()).classList.add('active');

  const filteredRows   = broker === 'All' ? allRows : allRows.filter(r => r.broker === broker);
  const filteredStocks = filteredRows.map(r => r.stock);
  const count          = filteredRows.length;

  // Reset stock visibility for new filter scope
  filteredStocks.forEach(s => stockVisible[s] = true);

  // 1. Holdings rows
  document.querySelectorAll('#holdings-tbody tr[data-broker]').forEach(tr => {
    tr.style.display = (broker === 'All' || tr.dataset.broker === broker) ? '' : 'none';
  });

  // 2. Chart + checkbox panel
  renderChart(filteredStocks);

  // 3. Today Gainers / Losers
  renderMovers(filteredRows);

  // 4. All-Time Gainers / Losers
  renderAllTimeMovers(filteredRows);

  // 5. Portfolio total
  renderTotal(filteredRows);

  // 6. Hero summary table
  renderHeroSummary(broker);

  // 7. Hero count + Holdings label
  document.getElementById('stock-count').textContent = count + ' stocks';
  document.getElementById('filter-label').textContent =
    broker === 'All' ? `All ${count} stocks` : `${count} ${broker} stocks`;

  // 8. Re-apply sort if active
  if (sortState.col) sortTable(sortState.col);

  // 9. Rebuild DMA table + scanner + exit radar for new broker scope
  if (_scannerCache) {
    renderScanner(_scannerCache);
    renderExitRadar(_scannerCache);
  }
  if (dmaLoaded) {
    dmaRows = buildDmaRows(_scannerCache || {});
    renderDmaTable(dmaRows);
  }
}

// ── Initial render ─────────────────────────────────────────────────────────
renderChart(stocks);
renderHeroSummary('All');
renderAllTimeMovers(allRows);
loadScanner();

// ── Delete confirmation ────────────────────────────────────────────────────
function confirmDelete(btn, stock) {
  if (confirm('Remove ' + stock + ' from portfolio?\\n\\nThis action cannot be undone.')) {
    btn.closest('form').submit();
  }
}

// ── Edit modal helper ──────────────────────────────────────────────────────
function openEdit(sym, qty, avg, yahoo, broker, buyDate) {
  document.getElementById('editSymbol').value        = sym;
  document.getElementById('editSymbolDisplay').value = sym;
  document.getElementById('editQty').value           = qty;
  document.getElementById('editAvg').value           = avg;
  document.getElementById('editYahoo').value         = yahoo;
  document.getElementById('editBroker').value        = broker || 'Zerodha';
  document.getElementById('editBuyDate').value       = buyDate || '';
  // reset the add-purchase section
  document.getElementById('apSymbol').value  = sym;
  document.getElementById('apQty').value     = '';
  document.getElementById('apPrice').value   = '';
  document.getElementById('apPreview').textContent =
    'Adds to the saved qty and recalculates the weighted average price.';
  new bootstrap.Modal(document.getElementById('editModal')).show();
}

// Live preview: old lot + new lot → combined qty and weighted average.
function previewAvg() {
  const oldQty = parseFloat(document.getElementById('editQty').value) || 0;
  const oldAvg = parseFloat(document.getElementById('editAvg').value) || 0;
  const aq     = parseFloat(document.getElementById('apQty').value)   || 0;
  const ap     = parseFloat(document.getElementById('apPrice').value) || 0;
  const out    = document.getElementById('apPreview');
  if (aq > 0 && ap > 0 && oldQty > 0) {
    const newQty = oldQty + aq;
    const newAvg = (oldQty * oldAvg + aq * ap) / newQty;
    out.innerHTML = `New holding: <b>${newQty}</b> shares @ avg <b>₹${newAvg.toFixed(2)}</b>`
      + ` &nbsp;<span style="opacity:.65">(was ${oldQty} @ ₹${oldAvg.toFixed(2)})</span>`;
  } else {
    out.textContent = 'Adds to the saved qty and recalculates the weighted average price.';
  }
}

// ── Holdings / Technical tab switcher ─────────────────────────────────────
let dmaLoaded = false;
let dmaRows   = [];
let dmaSortState = { col: null, dir: 1 };

function switchHoldingsTab(tab) {
  ['holdings','technical','watchlist'].forEach(t => {
    document.getElementById(t + '-panel').style.display = tab === t ? '' : 'none';
    document.getElementById('tab-' + t).classList.toggle('active', tab === t);
  });
  if (tab === 'technical' && !dmaLoaded) loadDmaData(false);
  if (tab === 'watchlist' && !wlLoaded)  loadWatchlistDma(false);
}

// ── DMA / Signal helpers ───────────────────────────────────────────────────
const SIGNAL_ORDER = {'Buy Setup':0,'Near Entry':1,'Watch':2,'Caution':3,'Avoid':4,'—':5};

// Client-side twin of the server's _dma_result_ok(): a usable result set has
// no error key and at least one symbol with a real CMP. Shared by the
// Technical tab, Signal Scanner and Watchlist so a transient yfinance failure
// is surfaced (with a retry) everywhere instead of silently blanking a view.
function dmaHasData(data) {
  return !!data && !data.error &&
         Object.values(data).some(v => v && v.cmp != null);
}

function loadDmaData(force) {
  const tbody = document.getElementById('dma-tbody');
  tbody.innerHTML = '<tr><td colspan="12" class="dma-loading">⏳ Fetching 1yr price history &amp; computing signals…</td></tr>';
  const url = '/api/dma' + (force ? '?force=1' : '');
  fetch(url)
    .then(r => r.json())
    .then(data => {
      if (!dmaHasData(data)) {
        dmaLoaded = false;   // allow a retry on next tab click / Refresh
        tbody.innerHTML = '<tr><td colspan="12" class="dma-loading text-danger">'
          + '⚠️ Could not fetch price data — Yahoo Finance may be rate-limiting. '
          + 'Click ⟳ Refresh DMA to retry.'
          + (data && data.error ? '<br><span style="font-size:.72rem;opacity:.7">' + data.error + '</span>' : '')
          + '</td></tr>';
        return;
      }
      dmaLoaded = true;
      _scannerCache = data;
      dmaRows = buildDmaRows(data);
      renderDmaTable(dmaRows);
      renderScanner(data);
      renderExitRadar(data);
    })
    .catch(err => {
      dmaLoaded = false;
      tbody.innerHTML = '<tr><td colspan="12" class="dma-loading text-danger">Error: ' + err + '</td></tr>';
    });
}

function buildDmaRows(data) {
  return allRows
    .filter(r => activeBroker === 'All' || r.broker === activeBroker)
    .map(r => {
      const d = data[r.stock] || {};
      return {
        stock:         r.stock,
        broker:        r.broker,
        cmp:           d.cmp           ?? null,
        ema20:         d.ema20         ?? null,
        dma50:         d.dma50         ?? null,
        dma200:        d.dma200        ?? null,
        dma200_slope:  d.dma200_slope  ?? null,
        ema_dist_pct:  d.ema_dist_pct  ?? null,
        rsi14:         d.rsi14         ?? null,
        vol_ratio:     d.vol_ratio     ?? null,
        off_high_pct:  d.off_high_pct  ?? null,
        cross:         d.cross         ?? '—',
        signal:        d.signal        ?? '—',
        stop:          d.stop          ?? null,
      };
    });
}

function dmaClass(cmp, ma) {
  if (cmp == null || ma == null) return '';
  return cmp > ma ? 'dma-above' : 'dma-below';
}

function slopeHtml(s) {
  if (s === 'up')   return '<span class="slope-up">↑ Up</span>';
  if (s === 'down') return '<span class="slope-dn">↓ Down</span>';
  if (s === 'flat') return '<span class="slope-flat">→ Flat</span>';
  return '<span style="color:#aaa">—</span>';
}

function volHtml(v) {
  if (v == null) return '<span style="color:#aaa">—</span>';
  const cls = v >= 1.5 ? 'vol-hot' : (v >= 1.2 ? 'vol-up' : (v <= 0.6 ? 'vol-low' : 'vol-neu'));
  return `<span class="${cls}">${v.toFixed(2)}×</span>`;
}

function offHighHtml(p) {
  if (p == null) return '<span style="color:#aaa">—</span>';
  const cls = p <= -20 ? 'neg' : (p <= -10 ? 'neu' : '');
  return `<span class="${cls}">${p.toFixed(1)}%</span>`;
}

// LTCG timer — Indian equity gains turn long-term (12.5% tax) after 365 days.
function ltcgHtml(buyDate) {
  if (!buyDate) return '<span style="color:#aaa" title="Set Buy Date via ✏️ Edit">—</span>';
  const held = Math.floor((Date.now() - new Date(buyDate).getTime()) / 86400000);
  if (isNaN(held) || held < 0) return '<span style="color:#aaa">—</span>';
  if (held >= 365) return '<span class="pos" title="Long-term — 12.5% tax">LT ✓</span>';
  const left = 365 - held;
  const cls  = left <= 60 ? 'neu' : '';   // amber when the boundary is near
  return `<span class="${cls}" title="Short-term (20% tax) until then">${left}d to LT</span>`;
}

function signalHtml(s) {
  const map = {
    'Buy Setup':  'sig-buy',
    'Near Entry': 'sig-near',
    'Watch':      'sig-watch',
    'Caution':    'sig-caut',
    'Avoid':      'sig-avoid',
  };
  const cls = map[s] || '';
  if (!cls) return '<span style="color:#aaa">—</span>';
  const icon = s === 'Buy Setup' ? '🟢 ' : s === 'Near Entry' ? '🟠 ' : s === 'Watch' ? '👀 ' : s === 'Caution' ? '🔴 ' : '⚫ ';
  return `<span class="${cls}">${icon}${s}</span>`;
}

function crossHtml(c) {
  if (!c || c === '—') return '<span style="color:#aaa">—</span>';
  return c.split(', ').map(part => {
    const cls = part.startsWith('↑') ? 'cross-up' : 'cross-dn';
    return `<span class="${cls}">${part}</span>`;
  }).join(' ');
}

function distHtml(v) {
  if (v == null) return '<span style="color:#aaa">—</span>';
  const cls = v > 0 ? 'dma-above' : v < -1 ? 'dma-below' : 'dma-neutral';
  const sign = v > 0 ? '+' : '';
  return `<span class="${cls}">${sign}${v.toFixed(2)}%</span>`;
}

function rsiHtml(v) {
  if (v == null) return '<span style="color:#aaa">—</span>';
  const cls = v < 30 ? 'rsi-os' : v > 70 ? 'rsi-ob' : 'rsi-neu';
  const tag = v < 30 ? ' ▲OS' : v > 70 ? ' ▼OB' : '';
  return `<span class="${cls}">${v.toFixed(1)}${tag}</span>`;
}

function fmt(v) {
  if (v == null) return '<span style="color:#aaa">—</span>';
  return '₹' + v.toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2});
}

function renderDmaTable(rows) {
  const tbody = document.getElementById('dma-tbody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="12" class="dma-loading">No data</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(r => `
    <tr data-broker="${r.broker}">
      <td class="fw-bold">${r.stock}</td>
      <td class="r">${fmt(r.cmp)}</td>
      <td class="r d-none d-md-table-cell ${dmaClass(r.cmp, r.ema20)}">${fmt(r.ema20)}</td>
      <td class="r d-none d-md-table-cell">${distHtml(r.ema_dist_pct)}</td>
      <td class="r">${rsiHtml(r.rsi14)}</td>
      <td class="r">${volHtml(r.vol_ratio)}</td>
      <td class="r d-none d-md-table-cell ${dmaClass(r.cmp, r.dma50)}">${fmt(r.dma50)}</td>
      <td class="r d-none d-md-table-cell ${dmaClass(r.cmp, r.dma200)}">${fmt(r.dma200)}</td>
      <td class="d-none d-md-table-cell">${slopeHtml(r.dma200_slope)}</td>
      <td class="d-none d-md-table-cell">${crossHtml(r.cross)}</td>
      <td>${signalHtml(r.signal)}</td>
      <td class="r d-none d-md-table-cell" style="color:#888">${fmt(r.stop)}</td>
    </tr>`).join('');
}

function sortDmaTable(col) {
  dmaSortState.dir = (dmaSortState.col === col) ? -dmaSortState.dir : 1;
  dmaSortState.col = col;
  document.querySelectorAll('#dma-table th.sortable').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.getAttribute('onclick') === `sortDmaTable('${col}')`)
      th.classList.add(dmaSortState.dir === 1 ? 'sort-asc' : 'sort-desc');
  });
  const sorted = [...dmaRows].sort((a, b) => {
    let va = a[col], vb = b[col];
    // Signal: sort by priority order
    if (col === 'signal') { va = SIGNAL_ORDER[va]??9; vb = SIGNAL_ORDER[vb]??9; }
    if (va == null && vb == null) return 0;
    if (va == null) return 1; if (vb == null) return -1;
    if (typeof va === 'string') return dmaSortState.dir * va.localeCompare(vb);
    return dmaSortState.dir * (va - vb);
  });
  renderDmaTable(sorted);
}

// ── Shared table-sort helpers (Scanner + Exit Radar) ────────────────────────
// Sorts with nulls always last, strings via localeCompare, numbers numerically.
function sortRowsBy(rows, dir, getVal) {
  return [...rows].sort((a, b) => {
    const va = getVal(a), vb = getVal(b);
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === 'string') return dir * va.localeCompare(vb);
    return dir * (va - vb);
  });
}

// Toggle the ▲/▼ indicator on the clicked header, clear the others.
function markSort(tableSel, onclickStr, dir) {
  document.querySelectorAll(tableSel + ' th.sortable').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.getAttribute('onclick') === onclickStr)
      th.classList.add(dir === 1 ? 'sort-asc' : 'sort-desc');
  });
}

function clearSort(tableSel) {
  document.querySelectorAll(tableSel + ' th.sortable')
    .forEach(th => th.classList.remove('sort-asc', 'sort-desc'));
}

// ── Signal Scanner ─────────────────────────────────────────────────────────
let _scannerCache = null;
let scannerRows   = [];
let scSortState   = { col: null, dir: 1 };
let scActive      = new Set();   // pill filters; empty = show all

function scGetter(col) {
  return col === 'signal' ? (r => SIGNAL_ORDER[r.signal] ?? 9)
       : col === 'size'   ? (r => sizeQty(r.cmp, r.stop))
       : (r => r[col]);
}

// Compose current sort + pill filters, then render.
function refreshScannerView() {
  let rows = scannerRows;
  if (scSortState.col) rows = sortRowsBy(rows, scSortState.dir, scGetter(scSortState.col));
  if (scActive.size)   rows = rows.filter(r => scActive.has(r.signal));
  renderScannerRows(rows,
    scActive.size ? 'No rows match the active filter — click the pill again to clear' : null);
}

function toggleScannerFilter(sig) {
  if (scActive.has(sig)) scActive.delete(sig); else scActive.add(sig);
  document.querySelectorAll('#scanner-pills .scanner-pill.clickable').forEach(p =>
    p.classList.toggle('on', scActive.has(p.dataset.sig)));
  refreshScannerView();
}

function renderScanner(data) {
  const allBrokerStocks = allRows.filter(r => activeBroker === 'All' || r.broker === activeBroker);
  scannerRows = allBrokerStocks
    .map(r => ({ stock: r.stock, broker: r.broker, ...( data[r.stock] || {}) }))
    .filter(r => ['Buy Setup','Near Entry','Watch'].includes(r.signal))
    .sort((a,b) => (SIGNAL_ORDER[a.signal]??9) - (SIGNAL_ORDER[b.signal]??9) || (a.ema_dist_pct??99) - (b.ema_dist_pct??99));
  scSortState = { col: null, dir: 1 };   // fresh data → default order, filters cleared
  scActive    = new Set();
  clearSort('#scanner-table');

  const buy  = scannerRows.filter(r => r.signal === 'Buy Setup').length;
  const near = scannerRows.filter(r => r.signal === 'Near Entry').length;
  const watch= scannerRows.filter(r => r.signal === 'Watch').length;

  document.getElementById('scanner-pills').innerHTML = `
    <span class="scanner-pill buy clickable"   data-sig="Buy Setup"  title="Click to filter" onclick="toggleScannerFilter('Buy Setup')">🟢 Buy Setup <strong>${buy}</strong></span>
    <span class="scanner-pill near clickable"  data-sig="Near Entry" title="Click to filter" onclick="toggleScannerFilter('Near Entry')">🟠 Near Entry <strong>${near}</strong></span>
    <span class="scanner-pill watch clickable" data-sig="Watch"      title="Click to filter" onclick="toggleScannerFilter('Watch')">👀 Watch <strong>${watch}</strong></span>
    <span style="font-size:.72rem;color:#888;margin-left:auto">
      Only Buy Setup, Near Entry &amp; Watch shown · ${allBrokerStocks.length} stocks scanned
    </span>`;

  renderScannerRows(scannerRows);
}

function renderScannerRows(rows, emptyMsg) {
  const tbody = document.getElementById('scanner-tbody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="12" class="text-center text-muted py-3">'
      + (emptyMsg || 'No actionable setups right now') + '</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td class="fw-bold">${r.stock}</td>
      <td class="r">${fmt(r.cmp)}</td>
      <td class="r d-none d-md-table-cell ${dmaClass(r.cmp, r.ema20)}">${fmt(r.ema20)}</td>
      <td class="r d-none d-md-table-cell">${distHtml(r.ema_dist_pct)}</td>
      <td class="r">${rsiHtml(r.rsi14)}</td>
      <td class="r">${volHtml(r.vol_ratio)}</td>
      <td class="r d-none d-md-table-cell ${dmaClass(r.cmp, r.dma50)}">${fmt(r.dma50)}</td>
      <td class="r d-none d-md-table-cell ${dmaClass(r.cmp, r.dma200)}">${fmt(r.dma200)}</td>
      <td class="d-none d-md-table-cell">${slopeHtml(r.dma200_slope)}</td>
      <td class="r d-none d-md-table-cell" style="color:#888">${fmt(r.stop)}</td>
      <td class="r d-none d-md-table-cell">${sizeHtml(r.cmp, r.stop)}</td>
      <td>${signalHtml(r.signal)}</td>
    </tr>`).join('');
}

function sortScannerTable(col) {
  scSortState.dir = (scSortState.col === col) ? -scSortState.dir : 1;
  scSortState.col = col;
  markSort('#scanner-table', `sortScannerTable('${col}')`, scSortState.dir);
  refreshScannerView();
}

// Suggested position size risking 1% of current portfolio value on the stop.
function sizeQty(cmp, stop) {
  if (cmp == null || stop == null || cmp <= stop) return null;
  const qty = Math.floor(calcStats(allRows).curr * 0.01 / (cmp - stop));
  return qty || null;
}

function sizeHtml(cmp, stop) {
  const qty = sizeQty(cmp, stop);
  if (qty == null) return '<span style="color:#aaa">—</span>';
  return `<span title="risk ₹${(cmp - stop).toFixed(2)}/share, ~₹${Math.round(qty * cmp).toLocaleString('en-IN')} outlay">${qty}</span>`;
}

// ── Exit Radar — holdings needing exit review ───────────────────────────────
let exitRows    = [];
let exSortState = { col: null, dir: 1 };
let exActive    = new Set();   // pill filters; empty = show all

// Pill categories — a row can match several, so filtering is a union (OR).
const EX_FILTERS = {
  stop:   x => x.belowStop,
  trend:  x => x.d.signal === 'Avoid' || x.d.signal === 'Caution',
  profit: x => x.takeProfit,
};

function refreshExitView() {
  let rows = exitRows;
  if (exSortState.col) {
    const get = EXIT_GETTERS[exSortState.col] || (x => x.d[exSortState.col]);
    rows = sortRowsBy(rows, exSortState.dir, get);
  }
  if (exActive.size)
    rows = rows.filter(x => [...exActive].some(k => EX_FILTERS[k](x)));
  renderExitRows(rows,
    exActive.size ? 'No rows match the active filter — click the pill again to clear' : null);
}

function toggleExitFilter(key) {
  if (exActive.has(key)) exActive.delete(key); else exActive.add(key);
  document.querySelectorAll('#exit-pills .scanner-pill.clickable').forEach(p =>
    p.classList.toggle('on', exActive.has(p.dataset.key)));
  refreshExitView();
}

// Column accessors for sorting the nested {r, d} row objects.
const EXIT_GETTERS = {
  stock:        x => x.r.stock,
  cmp:          x => x.d.cmp,
  pnl_pct:      x => x.r.pnl_pct,
  rsi14:        x => x.d.rsi14,
  vol_ratio:    x => x.d.vol_ratio,
  stop:         x => x.d.stop,
  off_high_pct: x => x.d.off_high_pct,
  ltcg:         x => {
    if (!x.r.buy_date) return null;
    const held = Math.floor((Date.now() - new Date(x.r.buy_date).getTime()) / 86400000);
    return isNaN(held) ? null : held;
  },
  signal:       x => SIGNAL_ORDER[x.d.signal] ?? 9,
  score:        x => x.score,
};

function renderExitRadar(data) {
  const pills = document.getElementById('exit-pills');
  const tbody = document.getElementById('exit-tbody');
  if (!pills || !tbody) return;

  const holdings = allRows.filter(r => activeBroker === 'All' || r.broker === activeBroker);
  const flagged = holdings.map(r => {
    const d = data[r.stock] || {};
    const belowStop = d.cmp != null && d.stop != null && d.cmp < d.stop;
    const takeProfit = d.rsi14 != null && d.rsi14 > 70 &&
                       d.ema_dist_pct != null && d.ema_dist_pct > 8;
    const reasons = [];
    let score = 0;
    if (belowStop)              { reasons.push('<span class="ex-badge ex-stop">Below stop</span>');    score += 2; }
    if (d.signal === 'Avoid')   { reasons.push('<span class="ex-badge ex-trend">Trend broken</span>'); score += 2; }
    else if (d.signal === 'Caution') { reasons.push('<span class="ex-badge ex-weak">Weak trend</span>'); score += 1; }
    if (takeProfit)             { reasons.push('<span class="ex-badge ex-profit">Take profit</span>'); score += 1; }
    return { r, d, reasons, score, belowStop, takeProfit };
  }).filter(x => x.reasons.length);

  flagged.sort((a, b) => b.score - a.score || (a.r.pnl_pct ?? 0) - (b.r.pnl_pct ?? 0));
  exitRows = flagged;
  exSortState = { col: null, dir: 1 };   // fresh data → severity order, filters cleared
  exActive    = new Set();
  clearSort('#exit-table');

  const nStop   = flagged.filter(x => x.belowStop).length;
  const nTrend  = flagged.filter(x => x.d.signal === 'Avoid' || x.d.signal === 'Caution').length;
  const nProfit = flagged.filter(x => x.takeProfit).length;
  pills.innerHTML = `
    <span class="scanner-pill clickable" data-key="stop"   title="Click to filter" onclick="toggleExitFilter('stop')"   style="background:#f5c6cb;color:#721c24">🔻 Below stop <strong>${nStop}</strong></span>
    <span class="scanner-pill clickable" data-key="trend"  title="Click to filter" onclick="toggleExitFilter('trend')"  style="background:#e2e3e5;color:#383d41">📉 Weak/broken trend <strong>${nTrend}</strong></span>
    <span class="scanner-pill clickable" data-key="profit" title="Click to filter" onclick="toggleExitFilter('profit')" style="background:#d4edda;color:#1c5e2e">💰 Take profit <strong>${nProfit}</strong></span>
    <span style="font-size:.72rem;color:#888;margin-left:auto">
      ${flagged.length} of ${holdings.length} holdings flagged
    </span>`;

  renderExitRows(exitRows);
}

function renderExitRows(rows, emptyMsg) {
  const tbody = document.getElementById('exit-tbody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted py-3">'
      + (emptyMsg || '✅ Nothing needs exit review right now') + '</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(({ r, d, reasons }) => {
    const pnlCls = r.pnl_pct != null ? (r.pnl_pct > 0 ? 'pos' : (r.pnl_pct < 0 ? 'neg' : '')) : '';
    const pnlTxt = r.pnl_pct != null ? (r.pnl_pct >= 0 ? '+' : '') + r.pnl_pct.toFixed(2) + '%' : '—';
    return `
    <tr>
      <td class="fw-bold">${r.stock}</td>
      <td class="r">${fmt(d.cmp)}</td>
      <td class="r ${pnlCls}">${pnlTxt}</td>
      <td class="r d-none d-md-table-cell">${rsiHtml(d.rsi14)}</td>
      <td class="r d-none d-md-table-cell">${volHtml(d.vol_ratio)}</td>
      <td class="r d-none d-md-table-cell" style="color:#888">${fmt(d.stop)}</td>
      <td class="r d-none d-md-table-cell">${offHighHtml(d.off_high_pct)}</td>
      <td class="r d-none d-md-table-cell">${ltcgHtml(r.buy_date)}</td>
      <td class="d-none d-md-table-cell">${signalHtml(d.signal)}</td>
      <td>${reasons.join('')}</td>
    </tr>`;
  }).join('');
}

function sortExitTable(col) {
  exSortState.dir = (exSortState.col === col) ? -exSortState.dir : 1;
  exSortState.col = col;
  markSort('#exit-table', `sortExitTable('${col}')`, exSortState.dir);
  refreshExitView();
}

function scannerFetchFailed(detail) {
  document.getElementById('scanner-pills').innerHTML =
    '<span style="font-size:.78rem;color:#c0392b">⚠️ Could not fetch signal data — '
    + 'Yahoo Finance may be rate-limiting.</span>'
    + '<button class="dma-refresh-btn" style="margin-left:auto" onclick="loadScanner()">⟳ Retry</button>';
  document.getElementById('scanner-tbody').innerHTML =
    '<tr><td colspan="12" class="text-center text-muted py-3">'
    + (detail ? 'Error: ' + detail : 'No data — click Retry above') + '</td></tr>';
  document.getElementById('exit-pills').innerHTML =
    '<span style="font-size:.78rem;color:#c0392b">⚠️ No data — retry from the scanner above.</span>';
  document.getElementById('exit-tbody').innerHTML =
    '<tr><td colspan="10" class="text-center text-muted py-3">No data</td></tr>';
}

function loadScanner() {
  fetch('/api/dma')
    .then(r => r.json())
    .then(data => {
      if (!dmaHasData(data)) {           // don't poison _scannerCache with an error payload
        scannerFetchFailed(data && data.error);
        return;
      }
      _scannerCache = data;
      renderScanner(data);
      renderExitRadar(data);
      // Update timestamp
      const ts = document.getElementById('scanner-ts');
      if (ts) ts.textContent = 'Data via yfinance';
    })
    .catch(err => scannerFetchFailed(err));
}

// ── Watchlist ──────────────────────────────────────────────────────────────
let wlLoaded    = false;
let wlSymbols   = {{ watchlist | tojson }};
let wlRows      = [];   // flat row objects for sorting
let wlSortState = { col: null, dir: 1 };

function loadWatchlistDma(force) {
  const tbody = document.getElementById('wl-tbody');
  tbody.innerHTML = '<tr><td colspan="13" class="dma-loading">⏳ Fetching 1yr data for watchlist…</td></tr>';
  const url = '/api/watchlist-dma' + (force ? '?force=1' : '');
  fetch(url)
    .then(r => r.json())
    .then(resp => {
      const syms = resp.symbols || [];
      // An empty watchlist is a valid (empty) result — only flag a failure
      // when symbols exist but the fetch produced no usable data.
      if (syms.length && !dmaHasData(resp.data)) {
        wlLoaded = false;   // allow a retry on next tab click / Refresh
        tbody.innerHTML = '<tr><td colspan="13" class="dma-loading text-danger">'
          + '⚠️ Could not fetch watchlist data — Yahoo Finance may be rate-limiting. '
          + 'Click ⟳ Refresh to retry.</td></tr>';
        return;
      }
      wlLoaded  = true;
      wlSymbols = syms;
      wlRows    = buildWlRows(resp.data || {}, wlSymbols);
      renderWlRows(wlRows);
    })
    .catch(err => {
      wlLoaded = false;
      tbody.innerHTML = `<tr><td colspan="13" class="dma-loading text-danger">Error: ${err}</td></tr>`;
    });
}

function buildWlRows(data, symbols) {
  return (symbols || []).map(sym => {
    const d = data[sym] || {};
    return {
      symbol:       sym,
      cmp:          d.cmp          ?? null,
      ema20:        d.ema20        ?? null,
      ema_dist_pct: d.ema_dist_pct ?? null,
      rsi14:        d.rsi14        ?? null,
      vol_ratio:    d.vol_ratio    ?? null,
      dma50:        d.dma50        ?? null,
      dma200:       d.dma200       ?? null,
      dma200_slope: d.dma200_slope ?? null,
      cross:        d.cross        ?? '—',
      signal:       d.signal       ?? '—',
      stop:         d.stop         ?? null,
    };
  });
}

function renderWlRows(rows) {
  const tbody = document.getElementById('wl-tbody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="13" class="text-center text-muted py-3">No watchlist stocks — add a Yahoo symbol above.</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(r => {
    const name = r.symbol.replace(/\\.(NS|BO)$/i, '');
    return `
    <tr>
      <td class="fw-bold">${name}<br><span style="font-size:.7rem;color:#888;font-weight:400">${r.symbol}</span></td>
      <td class="r">${fmt(r.cmp)}</td>
      <td class="r d-none d-md-table-cell ${dmaClass(r.cmp, r.ema20)}">${fmt(r.ema20)}</td>
      <td class="r d-none d-md-table-cell">${distHtml(r.ema_dist_pct)}</td>
      <td class="r">${rsiHtml(r.rsi14)}</td>
      <td class="r">${volHtml(r.vol_ratio)}</td>
      <td class="r d-none d-md-table-cell ${dmaClass(r.cmp, r.dma50)}">${fmt(r.dma50)}</td>
      <td class="r d-none d-md-table-cell ${dmaClass(r.cmp, r.dma200)}">${fmt(r.dma200)}</td>
      <td class="d-none d-md-table-cell">${slopeHtml(r.dma200_slope)}</td>
      <td class="d-none d-md-table-cell">${crossHtml(r.cross)}</td>
      <td>${signalHtml(r.signal)}</td>
      <td class="r d-none d-md-table-cell" style="color:#888">${fmt(r.stop)}</td>
      <td><button class="wl-remove-btn" title="Remove ${r.symbol}" onclick="wlRemove('${r.symbol}')">✕</button></td>
    </tr>`;
  }).join('');
}

function sortWlTable(col) {
  wlSortState.dir = (wlSortState.col === col) ? -wlSortState.dir : 1;
  wlSortState.col = col;
  document.querySelectorAll('#wl-table th.sortable').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.getAttribute('onclick') === `sortWlTable('${col}')`)
      th.classList.add(wlSortState.dir === 1 ? 'sort-asc' : 'sort-desc');
  });
  const sorted = [...wlRows].sort((a, b) => {
    let va = a[col], vb = b[col];
    if (col === 'signal') { va = SIGNAL_ORDER[va]??9; vb = SIGNAL_ORDER[vb]??9; }
    if (va == null && vb == null) return 0;
    if (va == null) return 1; if (vb == null) return -1;
    if (typeof va === 'string') return wlSortState.dir * va.localeCompare(vb);
    return wlSortState.dir * (va - vb);
  });
  renderWlRows(sorted);
}

// Legacy wrapper kept for compatibility
function renderWatchlistTable(data, symbols) {
  wlRows = buildWlRows(data, symbols);
  renderWlRows(wlRows);
}

function wlAdd() {
  const inp = document.getElementById('wl-input');
  const sym = inp.value.trim();
  if (!sym) return;
  const msg = document.getElementById('wl-msg');
  msg.textContent = 'Adding…';
  fetch('/watchlist/add', { method: 'POST', body: new URLSearchParams({ symbol: sym }) })
    .then(r => r.json())
    .then(resp => {
      if (resp.ok) {
        wlSymbols = resp.symbols;
        inp.value = '';
        msg.textContent = '✓ Added — refreshing…';
        setTimeout(() => { msg.textContent = ''; }, 3000);
        loadWatchlistDma(true);  // force-refresh so new symbol gets fetched
      }
    })
    .catch(() => { msg.textContent = 'Error adding'; });
}

function wlRemove(sym) {
  if (!confirm('Remove ' + sym + ' from watchlist?')) return;
  fetch('/watchlist/remove', { method: 'POST', body: new URLSearchParams({ symbol: sym }) })
    .then(r => r.json())
    .then(resp => {
      wlSymbols = resp.symbols;
      loadWatchlistDma(true);
    });
}

// ── Toast for Fetch Now ────────────────────────────────────────────────────
{% if fetch_msg == 'fetch_started' %}
const toast = document.getElementById('toastBar');
toast.style.display = 'block';
setTimeout(() => { toast.style.display = 'none'; }, 8000);
{% endif %}
</script>
</body>
</html>"""

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n  Portfolio Dashboard running at → http://localhost:5050\n")
    app.run(host="0.0.0.0", port=5050, debug=False)
