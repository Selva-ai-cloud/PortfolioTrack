# 📊 Portfolio Tracker

A self-hosted Indian stock portfolio tracker that auto-fetches EOD prices from Yahoo Finance and serves a live HTML dashboard with P&L analytics.

---

## ✨ Features

| Feature | Detail |
|---|---|
| **Auto price fetch** | yfinance batch download (NSE + BSE) — no API key needed |
| **Web dashboard** | Flask app at `http://localhost:5050` |
| **Daily P&L** | vs avg buy price, color-coded green / amber / red |
| **Day movers** | Top 3 gainers & losers by today's price vs yesterday |
| **P&L chart** | Line chart — all stocks, X = dates, Y = P&L % |
| **Holdings CRUD** | Add / edit / delete stocks directly from the browser |
| **Scheduled fetch** | Claude Routine fires `fetch_eod.py` at 3:35 PM Mon–Fri |
| **macOS notification** | Desktop alert after each successful fetch |

---

## 📁 File Structure

```
PortfolioTrack/
├── fetch_eod.py            # EOD price fetcher (yfinance → portfolio_history.json)
├── portfolio_app.py        # Flask web dashboard + CRUD API
├── holdings.json           # Holdings master data (edit via browser or directly)
├── portfolio_history.json  # Generated — daily price & P&L history (git-ignored)
├── portfolio_log.txt       # Runtime logs (git-ignored)
└── README.md
```

---

## 🚀 Quick Start

### 1 — Install dependencies (once)

```bash
pip3 install yfinance flask
```

### 2 — Configure holdings

Edit `holdings.json` directly, or use the **＋ Add Stock** button in the dashboard after starting the server.

```jsonc
{
  "RELIANCE": { "yahoo": "RELIANCE.NS", "avg": 2450.50, "qty": 10 }
}
```

> **Yahoo Finance symbol format:**
> - NSE stocks → `SYMBOL.NS`
> - BSE stocks → `SYMBOL.BO`

### 3 — Fetch today's prices

```bash
python3 fetch_eod.py
```

### 4 — Start the dashboard

```bash
python3 portfolio_app.py
```

Open **http://localhost:5050** in your browser.

---

## ⚙️ Auto-Fetch Setup (3:35 PM Mon–Fri)

The Claude Routine scheduler fires `fetch_eod.py` automatically at market close every weekday.

To set it up manually via launchd (macOS):

1. Copy `com.selva.portfolio.eod.plist` to `~/Library/LaunchAgents/`
2. Run:
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.selva.portfolio.eod.plist
```

---

## 🖥️ Dashboard Walkthrough

| Section | Description |
|---|---|
| **Top bar** | Last updated date · ⟳ Fetch Now · ＋ Add Stock |
| **Hero banner** | Net portfolio P&L vs avg buy price |
| **Top 3 Gainers** | Stocks with best daily move (today vs yesterday) |
| **Top 3 Losers** | Stocks with worst daily move |
| **P&L Chart** | Multi-line chart — each stock's cumulative P&L % over time |
| **Holdings table** | All stocks with Close, P&L ₹, P&L %, Day %, Edit/Delete |

---

## 📦 Holdings JSON Format

```jsonc
{
  "SYMBOL": {
    "yahoo": "SYMBOL.NS",    // Yahoo Finance ticker (null if unavailable)
    "avg":   250.50,         // Average buy price (₹)
    "qty":   100             // Number of shares held
  }
}
```

---

## 🔒 Security Notes

- `portfolio_history.json` and `.kite_config.json` are git-ignored — never commit price history or credentials.
- The Flask server binds to `0.0.0.0:5050` — accessible on your local network. Do not expose to the internet.

---

## 🛠️ Tech Stack

- **Python 3.14**
- **yfinance** — free Yahoo Finance data
- **Flask** — lightweight web server
- **Chart.js 4** — interactive line chart
- **Bootstrap 5** — responsive UI

---

*Auto-fetches at 3:35 PM IST, Mon–Fri via Claude Routines scheduler.*
