"""
Portfolio Web Dashboard
Run:  python3 portfolio_app.py
Open: http://localhost:5050
"""

import json, os, subprocess
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, url_for

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
HOLDINGS_FILE = os.path.join(BASE_DIR, "holdings.json")
HISTORY_FILE  = os.path.join(BASE_DIR, "portfolio_history.json")
FETCH_SCRIPT  = os.path.join(BASE_DIR, "fetch_eod.py")
PYTHON        = "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"

app = Flask(__name__)

# ── Data helpers ──────────────────────────────────────────────────────────────
def load_holdings():
    with open(HOLDINGS_FILE) as f:
        return json.load(f)

def save_holdings(h):
    with open(HOLDINGS_FILE, "w") as f:
        json.dump(h, f, indent=2)

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return {}

# ── Dashboard route ───────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    holdings     = load_holdings()
    history      = load_history()
    fetch_msg    = request.args.get("fetch_msg", "")

    dates        = sorted(history.keys())
    today_data   = history[dates[-1]] if dates else {}
    prev_data    = history[dates[-2]] if len(dates) > 1 else {}
    last_updated = dates[-1] if dates else "No data yet"

    # Build rows
    rows      = []
    total_pnl = 0.0
    for stock in sorted(holdings.keys()):
        info    = holdings[stock]
        td      = today_data.get(stock, {})
        close   = td.get("close")
        pnl_r   = td.get("pnl_rs")
        pnl_p   = td.get("pnl_pct")

        prev_close = prev_data.get(stock, {}).get("close")
        if close and prev_close:
            day_chg    = round((close - prev_close) / prev_close * 100, 2)
            day_chg_rs = round(close - prev_close, 2)
        else:
            day_chg = day_chg_rs = None

        if pnl_r:
            total_pnl += pnl_r

        rows.append({
            "stock":     stock,
            "exchange":  "BSE" if (info.get("yahoo") or "").endswith(".BO") else "NSE",
            "qty":       info["qty"],
            "avg":       info["avg"],
            "yahoo":     info.get("yahoo") or "",
            "close":     close,
            "pnl_rs":    pnl_r,
            "pnl_pct":   pnl_p,
            "day_chg":   day_chg,
            "day_chg_rs":day_chg_rs,
            "prev_close":prev_close,
        })

    # Top 3 gainers / losers by today's daily move
    scored = [(r["stock"], r["day_chg"]) for r in rows if r["day_chg"] is not None]
    if not scored:  # fallback: use overall pnl_pct on first day
        scored = [(r["stock"], r["pnl_pct"]) for r in rows if r["pnl_pct"] is not None]

    scored_desc = sorted(scored, key=lambda x: x[1], reverse=True)
    gainers = [next(r for r in rows if r["stock"] == s) | {"day_pct": p}
               for s, p in scored_desc[:3]]
    losers  = [next(r for r in rows if r["stock"] == s) | {"day_pct": p}
               for s, p in scored_desc[-3:][::-1]]

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
    h      = load_holdings()
    symbol = request.form["symbol"].strip().upper()
    yahoo  = request.form["yahoo"].strip() or None
    avg    = float(request.form["avg"])
    qty    = int(request.form["qty"])
    h[symbol] = {"yahoo": yahoo, "avg": avg, "qty": qty}
    save_holdings(h)
    return redirect(url_for("dashboard"))

@app.route("/update-stock", methods=["POST"])
def update_stock():
    h      = load_holdings()
    symbol = request.form["symbol"].strip().upper()
    if symbol in h:
        h[symbol]["qty"]   = int(request.form["qty"])
        h[symbol]["avg"]   = float(request.form["avg"])
        h[symbol]["yahoo"] = request.form["yahoo"].strip() or None
    save_holdings(h)
    return redirect(url_for("dashboard"))

@app.route("/delete-stock", methods=["POST"])
def delete_stock():
    h      = load_holdings()
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
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js"></script>
<style>
  body{background:#f0f2f5;font-family:'Segoe UI',Arial,sans-serif;font-size:0.88rem}
  .topbar{background:#1F3864;color:#fff;padding:10px 20px;display:flex;align-items:center;gap:12px;
          box-shadow:0 2px 6px rgba(0,0,0,.3)}
  .topbar .brand{font-size:1.1rem;font-weight:700;letter-spacing:.5px}
  .topbar .updated{opacity:.6;font-size:.8rem;flex:1}
  .card{border:none;border-radius:10px;box-shadow:0 1px 6px rgba(0,0,0,.09)}
  .card-hdr{background:#1F3864;color:#fff;font-weight:600;padding:10px 16px;border-radius:10px 10px 0 0;font-size:.9rem}
  .card-hdr-green{background:#375623;color:#fff;font-weight:600;padding:10px 16px;border-radius:10px 10px 0 0}
  .card-hdr-red{background:#9C0006;color:#fff;font-weight:600;padding:10px 16px;border-radius:10px 10px 0 0}
  .hero{background:linear-gradient(135deg,#1F3864 0%,#2d5ca8 100%);color:#fff;border-radius:12px;
        padding:20px 24px;box-shadow:0 3px 12px rgba(0,0,0,.2)}
  .hero .label{font-size:.78rem;opacity:.7;text-transform:uppercase;letter-spacing:.8px}
  .hero .amount{font-size:2rem;font-weight:800}
  .pos{color:#1c8c44;font-weight:600}  .neg{color:#c0392b;font-weight:600}  .neu{color:#8a6800;font-weight:600}
  .pos-bg{background:#C6EFCE!important}  .neg-bg{background:#FFC7CE!important}  .neu-bg{background:#FFEB9C!important}
  table thead th{background:#1F3864;color:#fff;border:none;white-space:nowrap;font-weight:500;
                 position:sticky;top:0;z-index:2;padding:8px 10px}
  table td{vertical-align:middle;padding:6px 10px;border-color:#e8e8e8}
  .tbl-wrap{max-height:480px;overflow-y:auto;border-radius:0 0 10px 10px}
  .total-row td{background:#1F3864!important;color:#fff!important;font-weight:700}
  .badge-bse{background:#17375e;color:#fff;font-size:.7rem;padding:2px 6px;border-radius:4px}
  .badge-nse{background:#1a5276;color:#fff;font-size:.7rem;padding:2px 6px;border-radius:4px}
  .btn-navy{background:#1F3864;color:#fff;border:none}
  .btn-navy:hover{background:#16305a;color:#fff}
  .btn-green{background:#375623;color:#fff;border:none}
  .btn-green:hover{background:#2d4a1e;color:#fff}
  .toast-bar{position:fixed;bottom:20px;right:20px;background:#375623;color:#fff;
             padding:12px 20px;border-radius:8px;font-weight:500;
             box-shadow:0 3px 10px rgba(0,0,0,.25);z-index:9999;display:none}
  .mover-card td{padding:7px 10px;vertical-align:middle}
  .sm-tbl thead th{background:#888;font-size:.8rem;padding:5px 8px}
  .green-tbl thead th{background:#375623}
  .red-tbl   thead th{background:#9C0006}
  #pnlChart{max-height:340px}
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

<div class="container-fluid px-3 py-3">

  <!-- Hero -->
  <div class="hero mb-3">
    <div class="row align-items-center">
      <div class="col-auto">
        <div class="label">Portfolio Net P&amp;L (vs Avg Buy)</div>
        <div class="amount {% if total_pnl >= 0 %}text-success{% else %}text-danger{% endif %}">
          {{ '+' if total_pnl >= 0 else '' }}₹{{ '{:,.0f}'.format(total_pnl) }}
        </div>
      </div>
      <div class="col text-end opacity-75">
        <div class="label">Holdings</div>
        <div class="fs-4 fw-bold">{{ rows|length }} stocks</div>
      </div>
    </div>
  </div>

  <!-- Gainers / Losers -->
  <div class="row g-3 mb-3">
    <div class="col-lg-6">
      <div class="card">
        <div class="card-hdr-green">📈 Top 3 Gainers — Today's Move</div>
        <div class="p-0">
          <table class="table table-sm mb-0 mover-card green-tbl">
            <thead><tr>
              <th>Stock</th><th>Prev ₹</th><th>Today ₹</th><th>Chg ₹</th><th>Chg %</th>
            </tr></thead>
            <tbody>
              {% for r in gainers %}
              <tr class="pos-bg">
                <td class="fw-bold">{{ r.stock }}</td>
                <td>{{ '₹{:,.2f}'.format(r.prev_close) if r.prev_close else '—' }}</td>
                <td>{{ '₹{:,.2f}'.format(r.close) if r.close else 'N/A' }}</td>
                <td>{{ ('+₹' if r.day_chg_rs and r.day_chg_rs >= 0 else '₹') + '{:,.2f}'.format(r.day_chg_rs) if r.day_chg_rs is not none else '—' }}</td>
                <td class="pos">{{ '+{:.2f}%'.format(r.day_pct) if r.day_pct >= 0 else '{:.2f}%'.format(r.day_pct) }}</td>
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
              <th>Stock</th><th>Prev ₹</th><th>Today ₹</th><th>Chg ₹</th><th>Chg %</th>
            </tr></thead>
            <tbody>
              {% for r in losers %}
              <tr class="neg-bg">
                <td class="fw-bold">{{ r.stock }}</td>
                <td>{{ '₹{:,.2f}'.format(r.prev_close) if r.prev_close else '—' }}</td>
                <td>{{ '₹{:,.2f}'.format(r.close) if r.close else 'N/A' }}</td>
                <td>{{ '₹{:,.2f}'.format(r.day_chg_rs) if r.day_chg_rs is not none else '—' }}</td>
                <td class="neg">{{ '{:.2f}%'.format(r.day_pct) }}</td>
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

  <!-- Line Chart -->
  <div class="card mb-3">
    <div class="card-hdr">📈 All Stocks — P&amp;L % by Day (vs Avg Buy Price)</div>
    <div class="card-body">
      <canvas id="pnlChart"></canvas>
    </div>
  </div>

  <!-- Holdings Table -->
  <div class="card mb-3">
    <div class="card-hdr">📋 Holdings</div>
    <div class="tbl-wrap">
      <table class="table table-hover table-sm mb-0">
        <thead>
          <tr>
            <th>Stock</th><th>Exch</th><th>Qty</th><th>Avg Buy ₹</th>
            <th>Close ₹</th><th>P&amp;L ₹</th><th>P&amp;L %</th><th>Day %</th><th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {% for r in rows %}
          {% if r.pnl_pct is not none %}
            {% if r.pnl_pct > 0.5 %}{% set rc = 'pos-bg' %}
            {% elif r.pnl_pct < -0.5 %}{% set rc = 'neg-bg' %}
            {% else %}{% set rc = 'neu-bg' %}{% endif %}
          {% else %}{% set rc = '' %}{% endif %}
          <tr class="{{ rc }}">
            <td class="fw-bold">{{ r.stock }}</td>
            <td><span class="badge-{{ r.exchange|lower }}">{{ r.exchange }}</span></td>
            <td>{{ r.qty }}</td>
            <td>₹{{ '{:,.2f}'.format(r.avg) }}</td>
            <td>{{ '₹{:,.2f}'.format(r.close) if r.close else 'N/A' }}</td>
            <td class="{{ 'pos' if r.pnl_rs and r.pnl_rs > 0 else ('neg' if r.pnl_rs and r.pnl_rs < 0 else '') }}">
              {% if r.pnl_rs is not none %}{{ '+' if r.pnl_rs >= 0 else '' }}₹{{ '{:,.0f}'.format(r.pnl_rs) }}{% else %}N/A{% endif %}
            </td>
            <td class="{{ 'pos' if r.pnl_pct and r.pnl_pct > 0 else ('neg' if r.pnl_pct and r.pnl_pct < 0 else '') }}">
              {% if r.pnl_pct is not none %}{{ '+' if r.pnl_pct >= 0 else '' }}{{ '{:.2f}'.format(r.pnl_pct) }}%{% else %}N/A{% endif %}
            </td>
            <td class="{{ 'pos' if r.day_chg and r.day_chg > 0 else ('neg' if r.day_chg and r.day_chg < 0 else '') }}">
              {% if r.day_chg is not none %}{{ '+' if r.day_chg >= 0 else '' }}{{ '{:.2f}'.format(r.day_chg) }}%{% else %}—{% endif %}
            </td>
            <td style="white-space:nowrap">
              <button class="btn btn-sm btn-outline-primary py-0 px-2"
                onclick="openEdit('{{ r.stock }}','{{ r.qty }}','{{ r.avg }}','{{ r.yahoo }}')">
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
            <td colspan="5">PORTFOLIO TOTAL</td>
            <td colspan="2" class="{{ 'text-success' if total_pnl >= 0 else 'text-danger' }}">
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
// ── Chart.js line chart ────────────────────────────────────────────────────
const rawData  = {{ chart_data | tojson }};
const dates    = {{ dates    | tojson }};
const stocks   = {{ stocks   | tojson }};

const PALETTE = [
  '#1f77b4','#ff7f0e','#2ca02c','#d62728','#9467bd','#8c564b','#e377c2',
  '#7f7f7f','#bcbd22','#17becf','#aec7e8','#ffbb78','#98df8a','#ff9896',
  '#c5b0d5','#c49c94','#f7b6d2','#c7c7c7','#dbdb8d','#9edae5','#393b79',
  '#637939','#8c6d31','#843c39','#7b4173','#5254a3','#6b6ecf','#b5cf6b'
];

const datasets = stocks.map((stock, i) => ({
  label: stock,
  data: dates.map(d => (rawData[d] && rawData[d][stock] != null) ? rawData[d][stock] : null),
  borderColor: PALETTE[i % PALETTE.length],
  backgroundColor: 'transparent',
  borderWidth: 1.5,
  pointRadius: dates.length <= 10 ? 3 : 1,
  tension: 0.2,
  spanGaps: true,
}));

new Chart(document.getElementById('pnlChart'), {
  type: 'line',
  data: { labels: dates, datasets },
  options: {
    responsive: true,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: {
        position: 'bottom',
        labels: { boxWidth: 10, font: { size: 10 }, padding: 8 }
      },
      tooltip: {
        callbacks: {
          label: ctx => `${ctx.dataset.label}: ${
            ctx.parsed.y != null ? ctx.parsed.y.toFixed(2) + '%' : 'N/A'}`
        }
      }
    },
    scales: {
      x: { title: { display: true, text: 'Date' }, ticks: { font: { size: 10 } } },
      y: {
        title: { display: true, text: 'P&L %' },
        ticks: { callback: v => v.toFixed(1) + '%', font: { size: 10 } },
        grid: { color: 'rgba(0,0,0,0.06)' }
      }
    }
  }
});

// ── Edit modal helper ──────────────────────────────────────────────────────
function openEdit(sym, qty, avg, yahoo) {
  document.getElementById('editSymbol').value        = sym;
  document.getElementById('editSymbolDisplay').value = sym;
  document.getElementById('editQty').value           = qty;
  document.getElementById('editAvg').value           = avg;
  document.getElementById('editYahoo').value         = yahoo;
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
