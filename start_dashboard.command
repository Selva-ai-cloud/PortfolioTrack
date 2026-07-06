#!/bin/bash
# Launcher for the Portfolio Tracker Flask dashboard.
cd "$HOME/Documents/claude/portfolio_tracker" || { echo "FOLDER NOT FOUND"; exit 1; }

# Don't start a second copy if one is already listening on 5050.
if curl -s -o /dev/null http://localhost:5050/ ; then
  echo "Already running on http://localhost:5050"
else
  # Flask output goes to its own log — portfolio_log.txt belongs to
  # fetch_eod.py (appended EOD history; '>' here used to wipe it).
  nohup python3 portfolio_app.py > flask.log 2>&1 &
  echo "Launched portfolio_app.py (pid $!)"
fi

# Give Flask a moment to bind, then verify from the real machine.
sleep 6
curl -s -o /dev/null -w "VERIFY: HTTP %{http_code}, %{size_download} bytes\n" http://localhost:5050/ > launch_verify.txt 2>&1
echo "---"
cat launch_verify.txt
echo "Dashboard: http://localhost:5050"
sleep 2
