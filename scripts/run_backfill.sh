#!/bin/zsh
# Waits for the governance job, then runs the resumable 5% backfill at low priority.
cd "$(dirname "$0")/.."
while pgrep -f "japanfilings.cli governance" >/dev/null; do sleep 60; done
exec nice -n 10 python3 -m japanfilings.backfill run --minutes 3000
