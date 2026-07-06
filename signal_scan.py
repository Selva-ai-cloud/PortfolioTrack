"""
Pre-close signal scan — run by launchd each weekday at 6:30 PM Adelaide time
(= 2:30 PM IST in Adelaide winter, ~1 hour before NSE close at 3:30 PM IST).

What it does:
  1. Computes fresh DMA/EMA signals for holdings + watchlist (same code the
     dashboard uses), based on the live intraday price Yahoo returns during
     market hours.
  2. Refreshes dma_cache.json / watchlist_dma_cache.json so the dashboard
     immediately shows the pre-close signals.
  3. Diffs each symbol's signal against the previous scan (signal_state.json)
     and fires a macOS notification for actionable transitions:
       - upgrades to Buy Setup / Near Entry  (entry side — holdings & watchlist)
       - downgrades to Caution / Avoid       (exit side — holdings only)
  4. On a failed fetch it notifies loudly instead of staying silent, and does
     NOT overwrite the previous state (so the transition fires next scan).

Run manually:  python3 signal_scan.py
"""
import json
import os
import subprocess
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import portfolio_app as app  # reuse compute_dma_data / _dma_result_ok / cache paths

STATE_FILE = os.path.join(BASE_DIR, "signal_state.json")

UPGRADES   = {"Buy Setup", "Near Entry"}
DOWNGRADES = {"Caution", "Avoid"}


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def notify(title, message):
    """macOS notification via osascript (runs in the user's GUI session).

    Title/message are passed as argv — never interpolated into the script —
    so emoji and quotes can't break AppleScript parsing.
    """
    script = ('on run argv\n'
              'display notification (item 2 of argv) '
              'with title (item 1 of argv) sound name "Glass"\n'
              'end run')
    try:
        subprocess.run(["osascript", "-e", script, title, message],
                       check=False, timeout=10)
    except Exception as e:
        log(f"notification failed: {e}")


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cache(path, data):
    """Same shape api_dma writes, so the dashboard serves this scan's result."""
    with open(path, "w") as f:
        json.dump({"fetched_at": datetime.now().isoformat(), "data": data}, f, indent=2)


def main():
    log("── Pre-close signal scan started ──")

    holdings_data  = app.compute_dma_data()
    watchlist_data = app.compute_watchlist_dma()

    holdings_ok  = app._dma_result_ok(holdings_data)
    watchlist_ok = app._dma_result_ok(watchlist_data)

    if not holdings_ok:
        err = holdings_data.get("error", "empty result") if isinstance(holdings_data, dict) else "bad result"
        log(f"holdings fetch FAILED ({err}) — keeping previous state")
        notify("⚠️ Portfolio scan failed",
               "Could not fetch price data (Yahoo rate-limit?). Signals not updated.")
        sys.exit(1)

    # Refresh dashboard caches with the pre-close signals
    save_cache(app.DMA_CACHE_FILE, holdings_data)
    if watchlist_ok:
        save_cache(app.WL_DMA_CACHE_FILE, watchlist_data)
    log(f"caches refreshed — {len(holdings_data)} holdings"
        + (f", {len(watchlist_data)} watchlist" if watchlist_ok else ", watchlist skipped"))

    # Diff signals vs previous scan. Keys are prefixed so a holding and a
    # watchlist symbol with the same name can't collide.
    prev = load_state()
    curr = {}
    transitions = []

    universe = [("H", k, v) for k, v in holdings_data.items()]
    if watchlist_ok:
        universe += [("W", k, v) for k, v in watchlist_data.items()]

    for kind, key, v in universe:
        sig  = (v or {}).get("signal") or "—"
        skey = f"{kind}:{key}"
        curr[skey] = sig
        old = prev.get(skey)
        # skip first-ever sighting, no-change, and insufficient-data noise
        if old is None or old == sig or "—" in (old, sig):
            continue
        if sig in UPGRADES:
            transitions.append(f"🟢 {key}: {old} → {sig}")
        elif sig in DOWNGRADES and kind == "H":  # exit-side alerts: holdings only
            transitions.append(f"🔴 {key}: {old} → {sig}")
        else:
            log(f"(minor) {key}: {old} → {sig}")

    with open(STATE_FILE, "w") as f:
        json.dump(curr, f, indent=2)

    if transitions:
        for t in transitions:
            log("ALERT " + t)
        shown = transitions[:4]
        extra = len(transitions) - len(shown)
        body  = " · ".join(shown) + (f" · …+{extra} more" if extra > 0 else "")
        notify(f"📊 Portfolio signals: {len(transitions)} change(s)", body)
    else:
        log("no signal transitions")

    log("── scan done ──")


if __name__ == "__main__":
    main()
