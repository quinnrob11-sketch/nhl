#!/usr/bin/env python3
"""
DraftKings NHL Player Props — via The Odds API (v2)
----------------------------------------------------
Pulls live DK lines + odds for tonight's NHL playoff games:
  - Shots on Goal  (player_shots_on_goal)   ✓ confirmed working
  - Points         (player_points)          ✓ confirmed working
  - Assists        (player_assists)         ✓ confirmed working
  - HITS:                                   ✗ Odds API does NOT carry NHL hits
                                              → fall back to manual entry / model defaults

Fixes from v1:
  1. Each market is queried separately (avoids batched-call failures when one market is invalid)
  2. Date filter widened to include all ET evening games (UTC commence may be next-day)
  3. player_total_hits removed; player_assists added

Usage:
    python scrape_odds.py [YYYY-MM-DD]

Output: odds_<date>.json
"""
import json, sys, time, os
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta

# Force UTF-8 stdout on Windows so unicode in player names doesn't crash prints
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

try:
    import requests
except ImportError:
    print("Install requests:  pip install requests"); sys.exit(1)

# ----- Load API key -----
def load_api_key():
    for i, a in enumerate(sys.argv):
        if a == "--key" and i+1 < len(sys.argv):
            return sys.argv[i+1]
    k = os.environ.get("ODDS_API_KEY")
    if k: return k
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("ODDS_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None

API_KEY = load_api_key()
if not API_KEY:
    print("ERROR: no ODDS_API_KEY found in .env, env, or --key arg")
    sys.exit(1)

BASE = "https://api.the-odds-api.com/v4"
SPORT = "icehockey_nhl"
BOOKMAKER = "draftkings"

# These are the player prop markets we KNOW work for NHL on The Odds API
MARKETS = ["player_shots_on_goal", "player_points", "player_assists"]
# NOT supported (returns 422): player_total_hits, player_hits

HEADERS = {"User-Agent": "nhl-model/2.0"}

def fetch_json(url, params=None):
    full_url = f"{url}?{urlencode(params or {})}"
    r = requests.get(full_url, headers=HEADERS, timeout=20)
    rem = r.headers.get("x-requests-remaining")
    used = r.headers.get("x-requests-used")
    if r.status_code != 200:
        return None, r.status_code, rem, r.text[:300]
    return r.json(), 200, rem, None

# Map team names to abbreviations
TEAM_ABBR = {
    "Anaheim Ducks":"ANA","Boston Bruins":"BOS","Buffalo Sabres":"BUF","Calgary Flames":"CGY","Carolina Hurricanes":"CAR",
    "Chicago Blackhawks":"CHI","Colorado Avalanche":"COL","Columbus Blue Jackets":"CBJ","Dallas Stars":"DAL","Detroit Red Wings":"DET",
    "Edmonton Oilers":"EDM","Florida Panthers":"FLA","Los Angeles Kings":"LAK","Minnesota Wild":"MIN","Montreal Canadiens":"MTL",
    "Montréal Canadiens":"MTL","Nashville Predators":"NSH","New Jersey Devils":"NJD","New York Islanders":"NYI","New York Rangers":"NYR",
    "Ottawa Senators":"OTT","Philadelphia Flyers":"PHI","Pittsburgh Penguins":"PIT","San Jose Sharks":"SJS","Seattle Kraken":"SEA",
    "St. Louis Blues":"STL","St Louis Blues":"STL","Tampa Bay Lightning":"TBL","Toronto Maple Leafs":"TOR","Utah Mammoth":"UTA",
    "Utah Hockey Club":"UTA","Vancouver Canucks":"VAN","Vegas Golden Knights":"VGK","Washington Capitals":"WSH","Winnipeg Jets":"WPG"
}

MARKET_TO_KIND = {
    "player_shots_on_goal": "sog",
    "player_points": "pts",
    "player_assists": "ast",
}

def is_evening_of_date(commence_iso, target_date):
    """Returns True if commence_time falls within the ET evening window of target_date.
    target_date is YYYY-MM-DD (assumed local ET).
    Window: target_date 16:00 UTC (~12pm ET start of slate) → next day 09:00 UTC (~5am ET).
    """
    try:
        c = datetime.fromisoformat(commence_iso.replace("Z","+00:00"))
        target = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        window_start = target + timedelta(hours=16)   # noon ET
        window_end = target + timedelta(hours=33)     # 5am ET next morning
        return window_start <= c <= window_end
    except Exception:
        return False

def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else time.strftime("%Y-%m-%d")
    print(f"=== DK NHL Player Props for {date_str} ===")

    # 1. List events
    print("1) Listing NHL events...")
    events, code, rem, err = fetch_json(f"{BASE}/sports/{SPORT}/events", {"apiKey": API_KEY})
    if code != 200:
        print(f"   FAILED: {code}  {err}"); sys.exit(1)
    print(f"   got {len(events)} events  (rem {rem})")

    # 2. Filter to target date
    today_events = [e for e in events if is_evening_of_date(e.get("commence_time",""), date_str)]
    if not today_events:
        print(f"   no events match {date_str} ET window — using ALL upcoming events")
        today_events = events[:6]  # cap to next 6 to save quota
    print(f"   filtered to {len(today_events)} events for {date_str}")
    for e in today_events:
        print(f"     - {e['away_team']} @ {e['home_team']} | {e['commence_time']}")

    out = {
        "scrapedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "date": date_str,
        "games": [],
        "props": [],
        "errors": []
    }

    for e in today_events:
        home_full = e["home_team"]; away_full = e["away_team"]
        home_abbr = TEAM_ABBR.get(home_full, home_full[:3].upper())
        away_abbr = TEAM_ABBR.get(away_full, away_full[:3].upper())
        game_str = f"{away_abbr}@{home_abbr}"
        out["games"].append({
            "id": e["id"], "home": home_abbr, "away": away_abbr,
            "home_full": home_full, "away_full": away_full,
            "commence": e.get("commence_time","")
        })
        print(f"\n2) {game_str}  (id {e['id'][:8]})")

        # Loop each market separately
        for market in MARKETS:
            data, code, rem, err = fetch_json(
                f"{BASE}/sports/{SPORT}/events/{e['id']}/odds",
                {"apiKey": API_KEY, "regions":"us", "markets":market, "bookmakers":BOOKMAKER, "oddsFormat":"american"}
            )
            if code != 200:
                print(f"   {market}: {code} skipped  ({err[:80] if err else ''})")
                out["errors"].append({"game": game_str, "market": market, "code": code})
                continue

            kind = MARKET_TO_KIND[market]
            n_outcomes = 0
            by_player = {}
            for bm in data.get("bookmakers", []):
                if bm.get("key") != BOOKMAKER: continue
                for m in bm.get("markets", []):
                    for o in m.get("outcomes", []):
                        n_outcomes += 1
                        name = o.get("description") or o.get("name") or ""
                        line = o.get("point")
                        side = o.get("name","").lower()
                        price = o.get("price")
                        key = (name, line)
                        if key not in by_player:
                            by_player[key] = {"player": name, "market": kind, "line": line, "game": game_str}
                        if "over" in side:
                            by_player[key]["over_price"] = price
                        elif "under" in side:
                            by_player[key]["under_price"] = price
            for v in by_player.values():
                out["props"].append(v)
            print(f"   {market}: {n_outcomes} outcomes -> {len(by_player)} props  (rem {rem})")
            time.sleep(0.4)

    # Output
    fn = f"odds_{date_str}.json"
    with open(fn, "w") as f:
        json.dump(out, f, indent=1)
    print(f"\nDONE wrote {fn}")
    print(f"   {len(out['games'])} games, {len(out['props'])} props")
    if out["errors"]:
        print(f"   {len(out['errors'])} errors (see file)")

    # Sample
    if out["props"]:
        # Show top high-volume
        sog = [p for p in out["props"] if p["market"]=="sog"]
        pts = [p for p in out["props"] if p["market"]=="pts"]
        ast = [p for p in out["props"] if p["market"]=="ast"]
        print(f"\n   SOG: {len(sog)}  PTS: {len(pts)}  AST: {len(ast)}")
        print(f"\n   Sample (SOG):")
        for p in sog[:8]:
            o = p.get("over_price","-"); u = p.get("under_price","-")
            print(f"     {p['player']:25s} O/U {p['line']}  ({o}/{u})  [{p['game']}]")

if __name__ == "__main__":
    main()
