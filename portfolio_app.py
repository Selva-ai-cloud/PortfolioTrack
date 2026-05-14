"""
Portfolio Web Dashboard
Run:  python3 portfolio_app.py
Open: http://localhost:5050
"""

import json
import os
import subprocess
from datetime import datetime

from flask import Flask, redirect, render_template_string, request, url_for

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOLDINGS_FILE = os.path.join(BASE_DIR, "holdings.json")
HISTORY_FILE = os.path.join(BASE_DIR, "portfolio_history.json")
FETCH_SCRIPT = os.path.join(BASE_DIR, "fetch_eod.py")
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


# ── Dashboard route ───────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    holdings = load_holdings()
    history = load_history()
    fetch_msg = request.args.get("fetch_msg", "")

    dates = sorted(history.keys())
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

    # Chart data: {date: {stock: pnl_pct}}
    chart_data = {}
    for d in dates:
        chart_data[d] = {
            s: v.get("pnl_pct")
            for s, v in history[d].items()
            if s != "_total_pnl" and v.get("pnl_pct") is not None
        }

    return render_template_string(
        HTML,
        rows=rows,
        total_pnl=round(total_pnl, 2),
        gainers=gainers,
        losers=losers,
        holdings=holdings,
        chart_data=chart_data,
        dates=dates,
        stocks=sorted(holdings.keys()),
        last_updated=last_updated,
        fetch_msg=fetch_msg,
    )


# ── Stock CRUD ────────────────────────────────────────────────────────────────
@app.route("/add-stock", methods=["POST"])
def add_stock():
    h = load_holdings()
    symbol = request.form["symbol"].strip().upper()
    yahoo = request.form["yahoo"].strip() or None
    avg = float(request.form["avg"])
    qty = int(request.form["qty"])
    broker = request.form.get("broker", "Zerodha")
    h[symbol] = {"yahoo": yahoo, "avg": avg, "qty": qty, "broker": broker}
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
          box-shadow:0 2px 6px rgba(0,0,0,.3)}
  .topbar .brand{font-size:1.1rem;font-weight:700;letter-spacing:.5px}
  .topbar .updated{opacity:.6;font-size:.8rem;flex:1}

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
  .tbl-wrap{max-height:480px;overflow-y:auto;border-radius:0 0 10px 10px}
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

  /* ── Chart container — explicit height so maintainAspectRatio:false works ── */
  .chart-wrap{position:relative;height:340px}

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

  <!-- Gainers / Losers -->
  <div class="row g-3 mb-3">
    <div class="col-lg-6">
      <div class="card">
        <div class="card-hdr-green">📈 Top 3 Gainers — Today's Move</div>
        <div class="p-0">
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
    <div class="col-lg-6">
      <div class="card">
        <div class="card-hdr-red">📉 Top 3 Losers — Today's Move</div>
        <div class="p-0">
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
    <div class="col-lg-6">
      <div class="card">
        <div class="card-hdr-green">🏆 All-Time Top 3 Gainers (vs Avg Buy)</div>
        <div class="p-0">
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
    <div class="col-lg-6">
      <div class="card">
        <div class="card-hdr-red">💔 All-Time Top 3 Losers (vs Avg Buy)</div>
        <div class="p-0">
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

  <!-- Holdings Table -->
  <div class="card mb-3">
    <div class="card-hdr d-flex align-items-center justify-content-between">
      <span>📋 Holdings</span>
      <span class="fw-normal opacity-75" style="font-size:.78rem" id="filter-label">All {{ rows|length }} stocks</span>
    </div>
    <div class="tbl-wrap">
      <table class="table table-hover table-sm mb-0">
        <thead>
          <tr>
            <th class="sortable" data-col="stock"    onclick="sortTable('stock')"   >Stock    <span class="si"></span></th>
            <th class="sortable" data-col="exchange" onclick="sortTable('exchange')" >Exch     <span class="si"></span></th>
            <th class="sortable" data-col="broker"   onclick="sortTable('broker')"  >Broker   <span class="si"></span></th>
            <th class="sortable r" data-col="qty"    onclick="sortTable('qty')"     >Qty      <span class="si"></span></th>
            <th class="sortable r" data-col="avg"    onclick="sortTable('avg')"     >Avg ₹    <span class="si"></span></th>
            <th class="sortable r" data-col="close"  onclick="sortTable('close')"   >Close ₹  <span class="si"></span></th>
            <th class="sortable r" data-col="pnl_rs" onclick="sortTable('pnl_rs')"  >P&amp;L ₹ <span class="si"></span></th>
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
            <td><span class="badge-{{ r.exchange|lower }}">{{ r.exchange }}</span></td>
            <td>{{ r.broker }}</td>
            <td class="r">{{ r.qty|int|string }}</td>
            <td class="r">₹{{ '{:,.2f}'.format(r.avg) }}</td>
            <td class="r">{{ '₹{:,.2f}'.format(r.close) if r.close else 'N/A' }}</td>
            <td class="r {{ 'pos' if r.pnl_rs and r.pnl_rs > 0 else ('neg' if r.pnl_rs and r.pnl_rs < 0 else '') }}">
              {% if r.pnl_rs is not none %}{{ '+' if r.pnl_rs >= 0 else '' }}₹{{ '{:,.0f}'.format(r.pnl_rs) }}{% else %}N/A{% endif %}
            </td>
            <td class="r {{ 'pos' if r.pnl_pct and r.pnl_pct > 0 else ('neg' if r.pnl_pct and r.pnl_pct < 0 else '') }}">
              {% if r.pnl_pct is not none %}{{ '+' if r.pnl_pct >= 0 else '' }}{{ '{:.2f}'.format(r.pnl_pct) }}%{% else %}N/A{% endif %}
            </td>
            <td class="r {{ 'pos' if r.day_chg and r.day_chg > 0 else ('neg' if r.day_chg and r.day_chg < 0 else '') }}">
              {% if r.day_chg is not none %}{{ '+' if r.day_chg >= 0 else '' }}{{ '{:.2f}'.format(r.day_chg) }}%{% else %}—{% endif %}
            </td>
            <td style="white-space:nowrap">
              <button class="btn btn-sm btn-outline-primary py-0 px-2 me-1"
                onclick="openEdit('{{ r.stock }}','{{ r.qty }}','{{ r.avg }}','{{ r.yahoo }}','{{ r.broker }}')">
                Edit
              </button>
              <form action="/delete-stock" method="post" class="d-inline"
                    onsubmit="return confirm('Remove {{ r.stock }} from portfolio?')">
                <input type="hidden" name="symbol" value="{{ r.stock }}">
                <button type="submit" class="btn btn-sm btn-outline-danger py-0 px-2">Del</button>
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
  </div>

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
    </div>
  </div>
</div>

<!-- Toast notification -->
<div class="toast-bar" id="toastBar">⟳ Fetching prices… refresh in ~10 seconds</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
<script>
// ── Raw data from server ───────────────────────────────────────────────────
const rawData = {{ chart_data | tojson }};
const dates   = {{ dates      | tojson }};
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
        label: ctx => `${ctx.dataset.label}: ${
          ctx.parsed.y != null ? ctx.parsed.y.toFixed(2) + '%' : 'N/A'}`
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

function renderChart(filteredStocks) {
  activeStocks = filteredStocks;
  if (pnlChart) pnlChart.destroy();
  pnlChart = new Chart(document.getElementById('pnlChart'), {
    type: 'line',
    data: { labels: dates, datasets: buildDatasets(filteredStocks) },
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
}

// ── Initial render ─────────────────────────────────────────────────────────
renderChart(stocks);
renderHeroSummary('All');
renderAllTimeMovers(allRows);

// ── Edit modal helper ──────────────────────────────────────────────────────
function openEdit(sym, qty, avg, yahoo, broker) {
  document.getElementById('editSymbol').value        = sym;
  document.getElementById('editSymbolDisplay').value = sym;
  document.getElementById('editQty').value           = qty;
  document.getElementById('editAvg').value           = avg;
  document.getElementById('editYahoo').value         = yahoo;
  document.getElementById('editBroker').value        = broker || 'Zerodha';
  new bootstrap.Modal(document.getElementById('editModal')).show();
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
