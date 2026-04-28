#!/usr/bin/env python3
"""
NHL Box-Score Scraper — per-date player stat lines
---------------------------------------------------
Pulls every skater's actual hits / shots / points from every playoff game
on a given date, for retrospective grading.

Usage:  python scrape_boxscores.py 2026-04-22
        (date format YYYY-MM-DD)

Output: boxscores_2026-04-22.json
"""
import json, sys, time
from urllib.parse import quote

try:
    import requests
except ImportError:
    print("Install requests:  pip install requests")
    sys.exit(1)

WEB = "https://api-web.nhle.com/v1/"
HEADERS = {"User-Agent": "nhl-model/1.0"}

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

def games_on_date(date_str):
    """Return list of gameIds played on date."""
    data = fetch(f"{WEB}schedule/{date_str}")
    games = []
    for week in data.get("gameWeek", []):
        if week.get("date") == date_str:
            for g in week.get("games", []):
                games.append({
                    "gameId": g["id"],
                    "away": g["awayTeam"]["abbrev"],
                    "home": g["homeTeam"]["abbrev"],
                    "awayScore": g["awayTeam"].get("score"),
                    "homeScore": g["homeTeam"].get("score"),
                    "gameType": g.get("gameType"),  # 2 = reg, 3 = playoffs
                    "gameState": g.get("gameState"),
                })
    return games

def boxscore(game_id):
    return fetch(f"{WEB}gamecenter/{game_id}/boxscore")

def main():
    if len(sys.argv) < 2:
        date_str = "2026-04-22"
        print(f"No date arg given — defaulting to {date_str}")
    else:
        date_str = sys.argv[1]

    print(f"=== Box scores for {date_str} ===")
    games = games_on_date(date_str)
    print(f"Found {len(games)} games")
    for g in games:
        print(f"  {g['away']} {g['awayScore']} @ {g['home']} {g['homeScore']}  ({g['gameState']})")

    out = {
        "date": date_str,
        "scrapedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "games": [],
    }

    for g in games:
        if g["gameState"] not in ("OFF", "FINAL"):
            print(f"  skipping {g['gameId']} — not final ({g['gameState']})")
            continue
        print(f"  pulling box for {g['away']}@{g['home']} (id {g['gameId']})...")
        try:
            bx = boxscore(g["gameId"])
        except Exception as e:
            print(f"    FAILED: {e}")
            continue

        # The boxscore schema under v1 typically has playerByGameStats with awayTeam/homeTeam/forwards/defense
        # Extract per-player hits, shots, goals, assists, points
        def extract_players(team_key):
            pbgs = (bx.get("playerByGameStats", {}) or {}).get(team_key, {}) or {}
            players = []
            for grp in ("forwards", "defense", "defensemen", "goalies"):
                for p in pbgs.get(grp, []) or []:
                    rec = {
                        "playerId": p.get("playerId"),
                        "name": (p.get("name",{}) or {}).get("default") or p.get("name"),
                        "sweater": p.get("sweaterNumber"),
                        "position": p.get("position"),
                        "goals": p.get("goals", 0) or 0,
                        "assists": p.get("assists", 0) or 0,
                        "points": (p.get("goals", 0) or 0) + (p.get("assists", 0) or 0),
                        "shots": p.get("sog") if p.get("sog") is not None else p.get("shots", 0),
                        "hits": p.get("hits", 0) or 0,
                        "blocked": p.get("blockedShots", 0) or p.get("blocks", 0) or 0,
                        "pim": p.get("pim", 0) or 0,
                        "toi": p.get("toi", "0:00"),
                    }
                    if grp == "goalies":
                        rec["saves"] = p.get("saves", 0)
                        rec["shotsAgainst"] = p.get("shotsAgainst", 0)
                        rec["goalsAgainst"] = p.get("goalsAgainst", 0)
                        rec["savePctg"] = p.get("savePctg")
                        rec["position"] = "G"
                    players.append(rec)
            return players

        out["games"].append({
            "gameId": g["gameId"],
            "away": g["away"], "home": g["home"],
            "awayScore": g["awayScore"], "homeScore": g["homeScore"],
            "away_players": extract_players("awayTeam"),
            "home_players": extract_players("homeTeam"),
        })

    fn = f"boxscores_{date_str}.json"
    with open(fn, "w") as f:
        json.dump(out, f, indent=1)
    print(f"\nDONE -> wrote {fn}")

if __name__ == "__main__":
    main()
