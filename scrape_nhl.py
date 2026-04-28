#!/usr/bin/env python3
"""
NHL Hits + SOG Model — Data Scraper
-----------------------------------
Pulls every stat the props model needs from the official NHL API:
  - Team per-60 rates (hits for/against, shots for/against) — regular season + 2026 playoffs
  - Skater per-game hits + SOG for all 6 team rosters tonight — reg season + playoffs
  - Current rosters + PP TOI share to flag PP1 skaters
  - League averages for the normalization terms

Output: nhl_data.json  (drop this in the same folder and the model will bake it in)

Run:  python scrape_nhl.py
Requires: requests   ->   pip install requests
"""
import json, sys, time
from urllib.parse import quote

try:
    import requests
except ImportError:
    print("Install requests first:  pip install requests")
    sys.exit(1)

SEASON = "20252026"
TEAMS_TONIGHT = ["BUF", "BOS", "CAR", "OTT", "COL", "LAK"]
STATS = "https://api.nhle.com/stats/rest/en/"
WEB   = "https://api-web.nhle.com/v1/"

HEADERS = {"User-Agent": "nhl-model/1.0"}

def fetch(url, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"  retry {i+1} after error: {e}")
            time.sleep(1.5)
    raise RuntimeError(f"failed: {url}")

def team_stats(gametype):
    """Team summary + realtime (hits/blocks) for one gametype."""
    exp = quote(f"seasonId={SEASON} and gameTypeId={gametype}")
    summary = fetch(f"{STATS}team/summary?limit=50&cayenneExp={exp}")["data"]
    realtime = fetch(f"{STATS}team/realtime?limit=50&cayenneExp={exp}")["data"]
    # merge by teamId
    r_by_id = {t["teamId"]: t for t in realtime}
    out = {}
    for t in summary:
        tid = t["teamId"]
        abbr = t.get("teamFullName", "")  # stats API returns full name; we key by abbreviation separately
        rt = r_by_id.get(tid, {})
        out[tid] = {
            "teamId": tid,
            "teamName": t.get("teamFullName"),
            "gamesPlayed": t.get("gamesPlayed", 0),
            "toi": t.get("gamesPlayed", 0) * 60,  # approx
            "goalsFor": t.get("goalsFor"),
            "goalsAgainst": t.get("goalsAgainst"),
            "shotsForPerGame": t.get("shotsForPerGame"),
            "shotsAgainstPerGame": t.get("shotsAgainstPerGame"),
            "hitsPerGame": rt.get("hitsPerGame"),
            # some endpoints expose hitsAgainstPerGame; fallback to None
            "hitsAgainstPerGame": rt.get("hitsAgainstPerGame"),
            "blockedShotsPerGame": rt.get("blockedShotsPerGame"),
        }
    return out

def skater_stats(gametype, kind="summary"):
    exp = quote(f"seasonId={SEASON} and gameTypeId={gametype}")
    all_rows = []
    start = 0
    while True:
        url = f"{STATS}skater/{kind}?limit=100&start={start}&cayenneExp={exp}"
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

def team_roster(abbr):
    url = f"{WEB}roster/{abbr}/current"
    return fetch(url)

def club_stats(abbr):
    """TOI per skater (incl. PP TOI) for current season — used to flag PP1."""
    url = f"{WEB}club-stats/{abbr}/now"
    return fetch(url)

def main():
    print("=== NHL Stats Scraper ===")
    out = {
        "season": SEASON,
        "scrapedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "teams_tonight": TEAMS_TONIGHT,
        "team_reg": {},
        "team_po": {},
        "skater_reg_summary": [],
        "skater_reg_realtime": [],
        "skater_po_summary": [],
        "skater_po_realtime": [],
        "rosters": {},
        "club_stats": {},
    }

    print("1) Team stats (reg season, gameTypeId=2)...")
    out["team_reg"] = team_stats(2)
    print(f"   got {len(out['team_reg'])} teams")

    print("2) Team stats (playoffs, gameTypeId=3)...")
    try:
        out["team_po"] = team_stats(3)
        print(f"   got {len(out['team_po'])} teams")
    except Exception as e:
        print(f"   playoff team stats unavailable: {e}")

    print("3) Skater summary (reg)...")
    out["skater_reg_summary"] = skater_stats(2, "summary")
    print(f"   got {len(out['skater_reg_summary'])} skaters")

    print("4) Skater realtime/hits (reg)...")
    out["skater_reg_realtime"] = skater_stats(2, "realtime")
    print(f"   got {len(out['skater_reg_realtime'])} skaters")

    print("5) Skater summary (playoffs)...")
    try:
        out["skater_po_summary"] = skater_stats(3, "summary")
        print(f"   got {len(out['skater_po_summary'])} skaters")
    except Exception as e:
        print(f"   po summary unavailable: {e}")

    print("6) Skater realtime/hits (playoffs)...")
    try:
        out["skater_po_realtime"] = skater_stats(3, "realtime")
        print(f"   got {len(out['skater_po_realtime'])} skaters")
    except Exception as e:
        print(f"   po realtime unavailable: {e}")

    print("7) Rosters for tonight's teams...")
    for abbr in TEAMS_TONIGHT:
        try:
            out["rosters"][abbr] = team_roster(abbr)
            print(f"   {abbr} roster ok")
        except Exception as e:
            print(f"   {abbr} roster failed: {e}")

    print("8) Club stats (for PP TOI / PP1 detection)...")
    for abbr in TEAMS_TONIGHT:
        try:
            out["club_stats"][abbr] = club_stats(abbr)
            print(f"   {abbr} club stats ok")
        except Exception as e:
            print(f"   {abbr} club stats failed: {e}")

    with open("nhl_data.json", "w") as f:
        json.dump(out, f, indent=1)
    print("")
    print(f"DONE -> wrote nhl_data.json ({sum(1 for _ in open('nhl_data.json'))} lines)")
    print("Drop this file in your nhl folder and tell Claude it's ready.")

if __name__ == "__main__":
    main()
