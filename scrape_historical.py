#!/usr/bin/env python3
"""
NHL Historical + Goalie Supplement Scraper
-------------------------------------------
Adds the two pieces the colleague's engine uses that ours was missing:
  1. Last season (2024-25) skater stats — for 50/50 historical blend baseline
  2. Goalie season summary — for GAA override on Points projections

Output: nhl_historical.json (drop next to nhl_data.json)
Run:    python scrape_historical.py
"""
import json, sys, time
from urllib.parse import quote

try:
    import requests
except ImportError:
    print("Install requests:  pip install requests")
    sys.exit(1)

HISTORICAL_SEASON = "20242025"  # last completed regular season
CURRENT_SEASON    = "20252026"
TEAMS_TONIGHT = ["BUF", "BOS", "CAR", "OTT", "COL", "LAK"]
STATS = "https://api.nhle.com/stats/rest/en/"
HEADERS = {"User-Agent": "nhl-model/1.1"}

def fetch(url, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"  retry {i+1}: {e}")
            time.sleep(1.5)
    raise RuntimeError(f"failed: {url}")

def paged(url_base, cayenne):
    """Paginated fetch for skater/goalie endpoints."""
    all_rows = []
    start = 0
    while True:
        url = f"{url_base}?limit=100&start={start}&cayenneExp={quote(cayenne)}"
        data = fetch(url)
        rows = data.get("data", [])
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < 100:
            break
        start += 100
        if start > 2000:
            break
    return all_rows

def historical_team_stats():
    """Last season team summary for True-Stat weighted blending."""
    exp = f"seasonId={HISTORICAL_SEASON} and gameTypeId=2"
    summary = fetch(f"{STATS}team/summary?limit=50&cayenneExp={quote(exp)}")["data"]
    return {t["teamId"]: t for t in summary}

def main():
    out = {
        "scrapedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "historical_season": HISTORICAL_SEASON,
        "current_season": CURRENT_SEASON,
        "historical_team_reg": {},
        "historical_skater_summary": [],
        "historical_skater_realtime": [],
        "goalie_current_reg": [],
        "goalie_current_po": [],
        "goalie_historical_reg": [],
    }

    print("1) Historical team stats (2024-25 reg season)...")
    out["historical_team_reg"] = historical_team_stats()
    print(f"   got {len(out['historical_team_reg'])} teams")

    print("2) Historical skater summary (2024-25 reg)...")
    out["historical_skater_summary"] = paged(f"{STATS}skater/summary",
        f"seasonId={HISTORICAL_SEASON} and gameTypeId=2")
    print(f"   got {len(out['historical_skater_summary'])} skaters")

    print("3) Historical skater realtime (2024-25 reg)...")
    out["historical_skater_realtime"] = paged(f"{STATS}skater/realtime",
        f"seasonId={HISTORICAL_SEASON} and gameTypeId=2")
    print(f"   got {len(out['historical_skater_realtime'])} skaters")

    print("4) Current season goalie summary (2025-26 reg)...")
    out["goalie_current_reg"] = paged(f"{STATS}goalie/summary",
        f"seasonId={CURRENT_SEASON} and gameTypeId=2")
    print(f"   got {len(out['goalie_current_reg'])} goalies")

    print("5) Current season goalie summary (2025-26 playoffs)...")
    try:
        out["goalie_current_po"] = paged(f"{STATS}goalie/summary",
            f"seasonId={CURRENT_SEASON} and gameTypeId=3")
        print(f"   got {len(out['goalie_current_po'])} goalies")
    except Exception as e:
        print(f"   playoff goalies skipped: {e}")

    print("6) Historical goalie summary (2024-25 reg)...")
    out["goalie_historical_reg"] = paged(f"{STATS}goalie/summary",
        f"seasonId={HISTORICAL_SEASON} and gameTypeId=2")
    print(f"   got {len(out['goalie_historical_reg'])} goalies")

    with open("nhl_historical.json", "w") as f:
        json.dump(out, f, indent=1)
    print(f"\nDONE -> wrote nhl_historical.json")
    print("Drop this file in your nhl folder and tell Claude it's ready.")

if __name__ == "__main__":
    main()
